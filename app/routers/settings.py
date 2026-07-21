import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.config import settings as app_settings
from app.core.db import get_core_db
from app.core.field_encryption import encrypt_secret, decrypt_secret
from app.core.security import generate_bot_token, get_current_user_id, hash_password, mask_token, verify_bot_token
from app.services import gmail as gmail_service

router = APIRouter(tags=["settings"])


class ValidateTokenBody(BaseModel):
    token: str


class UpdateStorageModeBody(BaseModel):
    storage_mode: str  # "hosted" | "own"
    mongo_url: str | None = None
    mongo_db_name: str | None = None


class UpdateAiProviderBody(BaseModel):
    ai_provider: str  # "ours" | "openai" | "groq" | "anthropic" | ...
    api_key: str | None = None


class AddCustomSiteBody(BaseModel):
    title: str
    url: str


class SiteCredentialBody(BaseModel):
    site: str
    needs_login: bool
    credential_mode: str  # "auto" | "manual"
    manual_username: str | None = None
    manual_password: str | None = None


def _mask_mongo_url(encrypted_url: str | None) -> str | None:
    if not encrypted_url:
        return None
    url = decrypt_secret(encrypted_url)
    if not url:
        return None
    # crude redaction of credentials in mongodb://user:pass@host form
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            _, host_part = rest.split("@", 1)
            return f"{scheme}://***:***@{host_part}"
    return url


@router.get("/settings")
async def get_settings(user_id: str = Depends(get_current_user_id)):
    db = get_core_db()
    doc = await db.settings.find_one({"user_id": user_id}) or {}

    return {
        "bot_online": doc.get("bot_online", False),
        "masked_bot_token": mask_token(doc["bot_token_plain_once"]) if doc.get("bot_token_plain_once") else None,
        "storage_mode": doc.get("storage_mode", "hosted"),
        "mongo_url_masked": _mask_mongo_url(doc.get("mongo_url_encrypted")),
        "ai_provider": doc.get("ai_provider", app_settings.default_ai_provider),
        "has_own_ai_key": bool(doc.get("ai_api_key")),
        "gmail_connected": doc.get("gmail_connected", False),
        "gmail_email": doc.get("gmail_email"),
        "gmail_last_checked": doc.get("gmail_last_checked"),
        # Numbers only — the actual application/chat content lives in the
        # user's own DB when storage_mode == "own"; these counters are
        # account-level metadata, so they belong in the hosted DB alongside
        # everything else in this document.
        "applications_count": doc.get("applications_count", 0),
        "chats_count": doc.get("chats_count", 0),
    }


@router.post("/overlay/regenerate-token")
async def regenerate_bot_token(user_id: str = Depends(get_current_user_id)):
    db = get_core_db()
    raw_token = generate_bot_token()
    await db.settings.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "bot_token_hash": hash_password(raw_token),
                # Stored only so we can render a masked preview in GET /settings.
                # The raw token itself is returned once here and never again.
                "bot_token_plain_once": raw_token,
                "bot_token_regenerated_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )
    # Returned once — the frontend shows this in a "copy now, you won't see it again" box.
    return {"token": raw_token}


@router.post("/overlay/validate-token")
async def validate_bot_token(body: ValidateTokenBody):
    """
    Called by the Electron bot's pairing screen (unauthenticated — the bot has no
    JWT, only this raw token) and again on every /ws/bot connect/reconnect.
    Since tokens are stored hashed, we can't look up by token value directly —
    we scan settings docs that have a bot_token_hash and verify against each.
    This is fine at CareerOS's current scale; if it becomes a bottleneck, switch
    to storing a fast lookup prefix/HMAC index alongside the bcrypt hash.
    """
    db = get_core_db()
    raw_token = body.token.strip()
    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    async for doc in db.settings.find({"bot_token_hash": {"$exists": True}}):
        if verify_bot_token(raw_token, doc["bot_token_hash"]):
            return {"valid": True, "user_id": doc["user_id"]}

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


@router.post("/settings/storage")
async def update_storage_mode(body: UpdateStorageModeBody, user_id: str = Depends(get_current_user_id)):
    db = get_core_db()
    if body.storage_mode not in ("hosted", "own"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="storage_mode must be 'hosted' or 'own'")
    if body.storage_mode == "own" and not body.mongo_url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="mongo_url is required for self-hosted storage")

    update = {"storage_mode": body.storage_mode}
    if body.storage_mode == "own":
        # Encrypted at rest — a Mongo connection string contains the DB
        # password in plaintext, so this can't be stored as-is (previously
        # it was, which was a real credentials-exposure gap).
        update["mongo_url_encrypted"] = encrypt_secret(body.mongo_url)
        if body.mongo_db_name:
            update["mongo_db_name"] = body.mongo_db_name
    await db.settings.update_one({"user_id": user_id}, {"$set": update}, upsert=True)
    return {"storage_mode": body.storage_mode}


@router.post("/settings/ai-provider")
async def update_ai_provider(body: UpdateAiProviderBody, user_id: str = Depends(get_current_user_id)):
    db = get_core_db()
    update = {"ai_provider": body.ai_provider}
    if body.api_key:
        update["ai_api_key"] = encrypt_secret(body.api_key)
    elif body.ai_provider == "ours":
        update["ai_api_key"] = None
    await db.settings.update_one({"user_id": user_id}, {"$set": update}, upsert=True)
    return {"ai_provider": body.ai_provider}


@router.get("/settings/gmail/connect")
async def get_gmail_connect_url(user_id: str = Depends(get_current_user_id)):
    if not app_settings.gmail_configured:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Gmail isn't configured yet on the server. Set GMAIL_CLIENT_ID/SECRET in .env.",
        )
    # `state` ties the OAuth callback back to this user; store it so /gmail/callback can verify it.
    db = get_core_db()
    state = secrets.token_urlsafe(24)
    await db.settings.update_one({"user_id": user_id}, {"$set": {"gmail_oauth_state": state}}, upsert=True)
    return {"oauth_url": gmail_service.build_oauth_url(state)}


@router.get("/settings/gmail/callback")
async def gmail_oauth_callback(code: str, state: str):
    db = get_core_db()
    doc = await db.settings.find_one({"gmail_oauth_state": state})
    if doc is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired OAuth state")

    tokens = await gmail_service.exchange_code_for_tokens(code)
    await db.settings.update_one(
        {"user_id": doc["user_id"]},
        {
            "$set": {
                "gmail_connected": True,
                "gmail_access_token": tokens.get("access_token"),
                "gmail_refresh_token": tokens.get("refresh_token"),
                "gmail_last_checked": datetime.now(timezone.utc),
            },
            "$unset": {"gmail_oauth_state": ""},
        },
    )
    # In production, redirect back to the frontend settings page instead of returning JSON.
    return {"connected": True}


@router.post("/settings/gmail/disconnect")
async def disconnect_gmail(user_id: str = Depends(get_current_user_id)):
    db = get_core_db()
    await db.settings.update_one(
        {"user_id": user_id},
        {
            "$set": {"gmail_connected": False, "gmail_email": None},
            "$unset": {"gmail_access_token": "", "gmail_refresh_token": ""},
        },
    )
    return {"gmail_connected": False}


@router.post("/gmail/scan")
async def trigger_gmail_scan(user_id: str = Depends(get_current_user_id)):
    db = get_core_db()
    doc = await db.settings.find_one({"user_id": user_id}) or {}
    if not doc.get("gmail_connected"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Gmail isn't connected yet")

    try:
        await gmail_service.scan_inbox_for_replies(doc.get("gmail_access_token", ""))
    except NotImplementedError as exc:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc))

    await db.settings.update_one({"user_id": user_id}, {"$set": {"gmail_last_checked": datetime.now(timezone.utc)}})
    return {"scanned": True}


@router.get("/settings/custom-sites")
async def list_custom_sites(user_id: str = Depends(get_current_user_id)):
    db = get_core_db()
    doc = await db.settings.find_one({"user_id": user_id}) or {}
    return {"custom_sites": doc.get("custom_sites") or []}


@router.post("/settings/custom-sites")
async def add_custom_site(body: AddCustomSiteBody, user_id: str = Depends(get_current_user_id)):
    title = body.title.strip()
    url = body.url.strip()
    if not title:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Title is required")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="URL must start with http:// or https://")

    db = get_core_db()
    doc = await db.settings.find_one({"user_id": user_id}) or {}
    custom_sites = doc.get("custom_sites") or []

    # Same title (case-insensitive) overwrites its URL instead of duplicating the tile.
    lowered = title.lower()
    custom_sites = [s for s in custom_sites if s.get("title", "").lower() != lowered]
    custom_sites.append({"title": title, "url": url})

    await db.settings.update_one({"user_id": user_id}, {"$set": {"custom_sites": custom_sites}}, upsert=True)
    return {"custom_sites": custom_sites}


@router.delete("/settings/custom-sites/{title}")
async def remove_custom_site(title: str, user_id: str = Depends(get_current_user_id)):
    db = get_core_db()
    doc = await db.settings.find_one({"user_id": user_id}) or {}
    custom_sites = doc.get("custom_sites") or []
    lowered = title.lower()
    remaining = [s for s in custom_sites if s.get("title", "").lower() != lowered]

    await db.settings.update_one({"user_id": user_id}, {"$set": {"custom_sites": remaining}}, upsert=True)
    return {"custom_sites": remaining}


# ---------- Per-site login credentials ----------
# Two modes, chosen per site by the user on the dashboard:
#   "auto"   — backend generates username/password from the user's own
#              account email (email-prefix + "@123K"), nothing stored beyond
#              the mode flag itself.
#   "manual" — user typed their own username/password for that specific
#              site; password is stored encrypted (Fernet, same key as
#              mongo_url/ai_api_key) and never returned in plaintext to the
#              frontend, only used server-side when the bot asks for it.

def _mask_username(username: str | None) -> str | None:
    if not username:
        return None
    if "@" in username:
        prefix, _, domain = username.partition("@")
        return f"{prefix[:2]}***@{domain}" if len(prefix) > 2 else f"***@{domain}"
    return f"{username[:2]}***" if len(username) > 2 else "***"


@router.get("/settings/site-credentials")
async def list_site_credentials(user_id: str = Depends(get_current_user_id)):
    db = get_core_db()
    doc = await db.settings.find_one({"user_id": user_id}) or {}
    creds = doc.get("site_credentials") or {}
    # Never leak the encrypted/plaintext password back to the client —
    # only enough for the UI to show "credentials saved" for that site.
    safe = {
        site: {
            "needs_login": c.get("needs_login", False),
            "credential_mode": c.get("credential_mode", "auto"),
            "manual_username": _mask_username(c.get("manual_username")),
            "has_manual_password": bool(c.get("manual_password_encrypted")),
        }
        for site, c in creds.items()
    }
    return {"site_credentials": safe}


@router.post("/settings/site-credentials")
async def set_site_credentials(body: SiteCredentialBody, user_id: str = Depends(get_current_user_id)):
    site_key = body.site.strip().lower()
    if not site_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Site is required")
    if body.credential_mode not in ("auto", "manual"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="credential_mode must be 'auto' or 'manual'")
    if body.credential_mode == "manual" and not (body.manual_username and body.manual_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Manual mode needs both username and password")

    db = get_core_db()
    doc = await db.settings.find_one({"user_id": user_id}) or {}
    creds = doc.get("site_credentials") or {}

    entry = {
        "needs_login": body.needs_login,
        "credential_mode": body.credential_mode,
    }
    if body.credential_mode == "manual":
        entry["manual_username"] = body.manual_username.strip()
        entry["manual_password_encrypted"] = encrypt_secret(body.manual_password)
    else:
        # Switching back to auto — drop any previously-saved manual creds
        # rather than leaving stale, unused ones sitting in the DB.
        entry["manual_username"] = None
        entry["manual_password_encrypted"] = None

    creds[site_key] = entry
    await db.settings.update_one({"user_id": user_id}, {"$set": {"site_credentials": creds}}, upsert=True)

    return {"site": site_key, "needs_login": entry["needs_login"], "credential_mode": entry["credential_mode"]}


@router.delete("/settings/site-credentials/{site}")
async def remove_site_credentials(site: str, user_id: str = Depends(get_current_user_id)):
    db = get_core_db()
    doc = await db.settings.find_one({"user_id": user_id}) or {}
    creds = doc.get("site_credentials") or {}
    creds.pop(site.strip().lower(), None)
    await db.settings.update_one({"user_id": user_id}, {"$set": {"site_credentials": creds}}, upsert=True)
    return {"ok": True}


async def resolve_login_credentials(user_id: str, site: str) -> dict | None:
    """
    Server-side only (called from ws.py, never exposed as an endpoint).
    Returns {"username": ..., "password": ...} or None if the site isn't
    configured for login, or manual creds are missing/corrupted.
    """
    db = get_core_db()
    doc = await db.settings.find_one({"user_id": user_id}) or {}
    creds = (doc.get("site_credentials") or {}).get(site.strip().lower())

    from bson import ObjectId
    try:
        user = await db.users.find_one({"_id": ObjectId(user_id)})
    except Exception:
        user = None

    if creds and creds.get("credential_mode") == "manual":
        password = decrypt_secret(creds.get("manual_password_encrypted") or "")
        if not creds.get("manual_username") or password is None:
            return None
        return {"username": creds["manual_username"], "password": password}

    # Auto mode (default, even if no explicit site_credentials entry exists
    # yet — a site can hit a login wall the user never pre-configured).
    if not user or not user.get("email"):
        return None
    email = user["email"]
    prefix = email.split("@")[0]
    return {"username": email, "password": f"{prefix}@123K"}
