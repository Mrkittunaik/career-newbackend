import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.config import settings as app_settings
from app.core.db import get_db
from app.core.field_encryption import encrypt_secret
from app.core.security import generate_bot_token, get_current_user_id, hash_password, mask_token, verify_bot_token
from app.services import gmail as gmail_service

router = APIRouter(tags=["settings"])


class ValidateTokenBody(BaseModel):
    token: str


class UpdateStorageModeBody(BaseModel):
    storage_mode: str  # "hosted" | "own"
    mongo_url: str | None = None


class UpdateAiProviderBody(BaseModel):
    ai_provider: str  # "ours" | "openai" | "groq" | "anthropic" | ...
    api_key: str | None = None


def _mask_mongo_url(url: str | None) -> str | None:
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
    db = get_db()
    doc = await db.settings.find_one({"user_id": user_id}) or {}

    return {
        "bot_online": doc.get("bot_online", False),
        "masked_bot_token": mask_token(doc["bot_token_plain_once"]) if doc.get("bot_token_plain_once") else None,
        "storage_mode": doc.get("storage_mode", "hosted"),
        "mongo_url_masked": _mask_mongo_url(doc.get("mongo_url")),
        "ai_provider": doc.get("ai_provider", app_settings.default_ai_provider),
        "has_own_ai_key": bool(doc.get("ai_api_key")),
        "gmail_connected": doc.get("gmail_connected", False),
        "gmail_email": doc.get("gmail_email"),
        "gmail_last_checked": doc.get("gmail_last_checked"),
    }


@router.post("/overlay/regenerate-token")
async def regenerate_bot_token(user_id: str = Depends(get_current_user_id)):
    db = get_db()
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
    db = get_db()
    raw_token = body.token.strip()
    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    async for doc in db.settings.find({"bot_token_hash": {"$exists": True}}):
        if verify_bot_token(raw_token, doc["bot_token_hash"]):
            return {"valid": True, "user_id": doc["user_id"]}

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


@router.post("/settings/storage")
async def update_storage_mode(body: UpdateStorageModeBody, user_id: str = Depends(get_current_user_id)):
    db = get_db()
    if body.storage_mode not in ("hosted", "own"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="storage_mode must be 'hosted' or 'own'")
    if body.storage_mode == "own" and not body.mongo_url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="mongo_url is required for self-hosted storage")

    update = {"storage_mode": body.storage_mode}
    if body.storage_mode == "own":
        update["mongo_url"] = body.mongo_url
        # NOTE: actually migrating this user's data to their own Mongo instance is a
        # background job left as a TODO — this only records the preference for now.
    await db.settings.update_one({"user_id": user_id}, {"$set": update}, upsert=True)
    return {"storage_mode": body.storage_mode}


@router.post("/settings/ai-provider")
async def update_ai_provider(body: UpdateAiProviderBody, user_id: str = Depends(get_current_user_id)):
    db = get_db()
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
    db = get_db()
    state = secrets.token_urlsafe(24)
    await db.settings.update_one({"user_id": user_id}, {"$set": {"gmail_oauth_state": state}}, upsert=True)
    return {"oauth_url": gmail_service.build_oauth_url(state)}


@router.get("/settings/gmail/callback")
async def gmail_oauth_callback(code: str, state: str):
    db = get_db()
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
    db = get_db()
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
    db = get_db()
    doc = await db.settings.find_one({"user_id": user_id}) or {}
    if not doc.get("gmail_connected"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Gmail isn't connected yet")

    try:
        await gmail_service.scan_inbox_for_replies(doc.get("gmail_access_token", ""))
    except NotImplementedError as exc:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc))

    await db.settings.update_one({"user_id": user_id}, {"$set": {"gmail_last_checked": datetime.now(timezone.utc)}})
    return {"scanned": True}
