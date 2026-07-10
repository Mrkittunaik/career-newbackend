import base64
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from app.core.db import get_shared_db, get_user_db
from app.core.security import decrypt_value, encrypt_value
from app.ws.dashboard_manager import manager as dashboard_manager

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

# Refresh a bit early rather than exactly at expiry, to avoid a request
# racing an access token that expires mid-flight.
_TOKEN_REFRESH_SKEW = timedelta(minutes=2)

# Default lookback window when a connection has never been scanned before.
_DEFAULT_SCAN_LOOKBACK_DAYS = 7


class GmailClient:
    """Thin authenticated wrapper around the Gmail REST API for one user.
    Deliberately not using google-api-python-client to keep this dependency-light
    and consistent with the httpx-based approach already used in
    app.services.gmail_oauth_service."""

    def __init__(self, access_token: str):
        self._access_token = access_token

    async def list_message_ids(self, query: str, max_results: int = 50) -> list[str]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{GMAIL_API_BASE}/messages",
                headers={"Authorization": f"Bearer {self._access_token}"},
                params={"q": query, "maxResults": max_results},
            )
            response.raise_for_status()
            data = response.json()
            return [item["id"] for item in data.get("messages", [])]

    async def get_message(self, message_id: str) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{GMAIL_API_BASE}/messages/{message_id}",
                headers={"Authorization": f"Bearer {self._access_token}"},
                params={"format": "metadata", "metadataHeaders": ["From", "Subject"]},
            )
            response.raise_for_status()
            return response.json()


def _extract_header(message: dict, header_name: str) -> str:
    headers = message.get("payload", {}).get("headers", [])
    for header in headers:
        if header.get("name", "").lower() == header_name.lower():
            return header.get("value", "")
    return ""


def _extract_sender_domain(from_header: str) -> str:
    """'Acme Recruiting <hr@acme.com>' -> 'acme.com'. Falls back to '' if no
    email-shaped address is found."""
    match = re.search(r"@([\w.-]+)", from_header)
    return match.group(1).lower() if match else ""


async def _refresh_access_token(refresh_token: str) -> dict:
    payload = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(GOOGLE_TOKEN_URL, data=payload)
        response.raise_for_status()
        return response.json()


async def get_gmail_client(user_id: str, shared_db: Optional[AsyncIOMotorDatabase] = None) -> Optional[GmailClient]:
    """Loads gmail_connections for user_id, decrypts the stored tokens, refreshes
    the access token if it's expired (or about to be), persists the refreshed
    token back encrypted, and returns an authenticated GmailClient.

    Returns None if the user has no gmail connection -- callers should treat
    that as "nothing to do" rather than an error, since Gmail is opt-in.
    """
    db = shared_db or get_shared_db()
    connection_doc = await db["gmail_connections"].find_one({"user_id": user_id})
    if not connection_doc:
        return None

    tokens = connection_doc.get("oauth_tokens", {})
    access_token_encrypted = tokens.get("access_token_encrypted")
    refresh_token_encrypted = tokens.get("refresh_token_encrypted")
    expiry = tokens.get("expiry")

    if not access_token_encrypted:
        return None

    needs_refresh = False
    if expiry is not None:
        if isinstance(expiry, str):
            expiry = datetime.fromisoformat(expiry)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) + _TOKEN_REFRESH_SKEW >= expiry:
            needs_refresh = True

    if needs_refresh:
        if not refresh_token_encrypted:
            # expired and nothing to refresh with -- connection is dead until
            # the user re-consents via /settings/gmail/connect
            return None

        refresh_token = decrypt_value(refresh_token_encrypted)
        refreshed = await _refresh_access_token(refresh_token)

        new_access_token = refreshed["access_token"]
        new_expiry = datetime.now(timezone.utc) + timedelta(seconds=refreshed.get("expires_in", 3600))

        await db["gmail_connections"].update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "oauth_tokens.access_token_encrypted": encrypt_value(new_access_token),
                    "oauth_tokens.expiry": new_expiry,
                }
            },
        )

        return GmailClient(access_token=new_access_token)

    access_token = decrypt_value(access_token_encrypted)
    return GmailClient(access_token=access_token)


def match_email_to_application(
    from_header: str,
    subject: str,
    applications: list[dict],
) -> Optional[dict]:
    """Matching strategy lives entirely in this function so it can be improved
    later (e.g. tracking pixel IDs, ATS-specific subject patterns, ML
    classification) without touching the scan loop in scan_inbox_for_replies.

    Current strategy (simple, low false-positive-rate heuristics):
      1. sender domain contains the company name (normalized), OR
      2. subject line contains the company name, OR
      3. subject line contains common application-reply keywords AND the
         company name also appears somewhere in the subject or sender.

    Returns the matched job_application dict, or None.
    """
    sender_domain = _extract_sender_domain(from_header)
    subject_lower = subject.lower()

    reply_keywords = (
        "application", "interview", "next steps", "thank you for applying",
        "we received your application", "position", "candidacy", "role",
    )

    for application in applications:
        company = (application.get("company") or "").strip().lower()
        if not company:
            continue

        # normalize company name to a bare token for domain/substring matching
        # e.g. "Acme, Inc." -> "acme"
        company_token = re.sub(r"[^a-z0-9]", "", company.split(",")[0].split(" ")[0])
        if not company_token:
            continue

        domain_match = bool(sender_domain) and company_token in sender_domain.replace(".", "")
        subject_company_match = company in subject_lower or company_token in subject_lower.replace(" ", "")

        if domain_match or subject_company_match:
            return application

        # weaker signal: generic reply keyword + company mentioned anywhere
        has_keyword = any(keyword in subject_lower for keyword in reply_keywords)
        if has_keyword and (company_token in sender_domain or company in subject_lower):
            return application

    return None


async def scan_inbox_for_replies(user_id: str) -> dict:
    """Fetches recent Gmail messages for user_id (since last_scanned_at, or the
    last _DEFAULT_SCAN_LOOKBACK_DAYS days on first scan), matches each against
    the user's job_applications, and records any matches found. Always updates
    last_scanned_at on completion (even if 0 matches), so the next scan doesn't
    reprocess the same window.

    Returns a small summary dict for logging/manual-trigger responses.
    """
    shared_db = get_shared_db()

    connection_doc = await shared_db["gmail_connections"].find_one({"user_id": user_id})
    if not connection_doc:
        return {"scanned": 0, "matched": 0, "skipped_reason": "not_connected"}

    gmail_client = await get_gmail_client(user_id, shared_db=shared_db)
    if gmail_client is None:
        return {"scanned": 0, "matched": 0, "skipped_reason": "token_unavailable"}

    last_scanned_at = connection_doc.get("last_scanned_at")
    if last_scanned_at:
        if isinstance(last_scanned_at, str):
            last_scanned_at = datetime.fromisoformat(last_scanned_at)
        since = last_scanned_at
    else:
        since = datetime.now(timezone.utc) - timedelta(days=_DEFAULT_SCAN_LOOKBACK_DAYS)

    # Gmail search query uses day-granularity 'after:' (unix seconds also works
    # and is more precise, so we use that to avoid re-scanning a whole day
    # every run)
    after_epoch = int(since.timestamp())
    query = f"after:{after_epoch} category:primary"

    settings_dict = {"user_id": user_id}
    settings_doc = await shared_db["user_settings"].find_one({"user_id": user_id})
    if settings_doc:
        settings_dict.update(settings_doc)
    user_db = await get_user_db(settings_dict)

    scan_time = datetime.now(timezone.utc)
    matched_count = 0

    try:
        message_ids = await gmail_client.list_message_ids(query=query)

        if message_ids:
            # only need submitted applications -- skipped/failed ones were
            # never sent, so no employer reply is possible for them
            applications = await user_db["job_applications"].find(
                {"user_id": user_id, "status": "submitted"}
            ).to_list(length=None)

            for message_id in message_ids:
                message = await gmail_client.get_message(message_id)
                from_header = _extract_header(message, "From")
                subject = _extract_header(message, "Subject")

                matched_application = match_email_to_application(from_header, subject, applications)
                if matched_application is None:
                    continue

                snippet = (message.get("snippet") or subject or "")[:280]

                await user_db["job_applications"].update_one(
                    {"_id": matched_application["_id"]},
                    {
                        "$set": {
                            "reply_received": True,
                            "reply_snippet": snippet,
                            "reply_received_at": scan_time,
                        }
                    },
                )
                matched_count += 1

                await dashboard_manager.push_to_user(
                    user_id,
                    {
                        "type": "application_reply_received",
                        "job_application_id": str(matched_application["_id"]),
                        "reply_snippet": snippet,
                    },
                )
        scan_summary = {"scanned": len(message_ids), "matched": matched_count}
    finally:
        # always advance the watermark, even on partial failure mid-loop,
        # so a single bad message doesn't stall the whole connection forever
        # on a retry -- worst case we miss one message, which is preferable
        # to reprocessing the same growing window every run indefinitely
        await shared_db["gmail_connections"].update_one(
            {"user_id": user_id},
            {"$set": {"last_scanned_at": scan_time}},
        )

    return scan_summary


async def scan_all_connected_users() -> dict:
    """Runs scan_inbox_for_replies for every user with an active gmail_connections
    doc. Called by the periodic background job in app.routes.gmail.

    NOTE: this loops sequentially in-process. Fine at small scale, but if the
    number of connected users grows, this should move to a proper task queue
    (Celery/RQ) with one job per user so a slow/stuck Gmail API call for one
    user can't delay everyone else's scan.
    """
    shared_db = get_shared_db()
    connections = await shared_db["gmail_connections"].find({}, {"user_id": 1}).to_list(length=None)

    total_matched = 0
    for connection in connections:
        user_id = connection["user_id"]
        try:
            result = await scan_inbox_for_replies(user_id)
            total_matched += result.get("matched", 0)
        except Exception:
            # one user's scan failing (revoked access, API error, etc.)
            # must not abort the batch for everyone else
            continue

    return {"users_scanned": len(connections), "total_matched": total_matched}
