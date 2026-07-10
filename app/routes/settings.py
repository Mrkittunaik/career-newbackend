from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.core.config import settings as app_settings
from app.core.db import evict_user_db, get_db
from app.core.security import decrypt_value, encrypt_value, get_current_user
from app.schemas.settings import AIProviderSettingsUpdate, StorageSettingsUpdate
from app.schemas.settings_response import (
    GmailConnectResponse,
    SettingsResponse,
    StorageTestFailedResponse,
    StorageTestResult,
)
from app.services.gmail_oauth_service import exchange_code_for_tokens, save_gmail_connection

router = APIRouter(prefix="/settings", tags=["settings"])


def _mask_secret(raw_value: Optional[str]) -> Optional[str]:
    """Shows only the last 4 chars (e.g. '••••ab12'). None/empty stays None
    so the frontend can tell 'not set' apart from 'set but masked'."""
    if not raw_value:
        return None
    if len(raw_value) <= 4:
        return "•" * len(raw_value)
    return "•" * 4 + raw_value[-4:]


async def _test_mongo_connection(mongo_url: str) -> StorageTestResult:
    """Attempts a real motor client ping against the candidate mongo_url.
    Always closes the probe client -- this is a throwaway connection, never
    the cached per-user client from app.core.db."""
    probe_client: Optional[AsyncIOMotorClient] = None
    try:
        probe_client = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=5000)
        await probe_client.admin.command("ping")
        return StorageTestResult(success=True, message="Connection succeeded")
    except Exception as exc:
        # Don't leak the raw mongo_url (which may contain credentials) into
        # the error message returned to the client or logged anywhere.
        return StorageTestResult(success=False, message=f"Connection failed: {type(exc).__name__}")
    finally:
        if probe_client is not None:
            probe_client.close()


@router.post("/storage")
async def update_storage_settings(
    body: StorageSettingsUpdate,
    current_user: dict = Depends(get_current_user),
    shared_db: AsyncIOMotorDatabase = Depends(get_db),
):
    user_id = str(current_user["_id"])

    if body.storage_mode == "own":
        if not body.mongo_url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="mongo_url is required when storage_mode is 'own'",
            )

        test_result = await _test_mongo_connection(body.mongo_url)
        if not test_result.success:
            # Explicit, clear failure -- nothing is saved if the test fails.
            return StorageTestFailedResponse(
                success=False,
                storage_mode=body.storage_mode,
                message=test_result.message,
            )

        update_fields = {
            "storage_mode": body.storage_mode,
            "mongo_url_encrypted": encrypt_value(body.mongo_url),
            "updated_at": datetime.now(timezone.utc),
        }
    else:
        # switching back to "shared" -- clear any previously stored own-mongo
        # url so it's never left dangling/decryptable once unused
        update_fields = {
            "storage_mode": body.storage_mode,
            "mongo_url_encrypted": None,
            "updated_at": datetime.now(timezone.utc),
        }

    await shared_db["user_settings"].update_one(
        {"user_id": user_id},
        {"$set": update_fields, "$setOnInsert": {"user_id": user_id}},
        upsert=True,
    )

    # the cached AsyncIOMotorClient in app.core.db for this user (if any) is
    # now stale -- evict it so the next get_user_db() call reconnects using
    # the freshly saved storage_mode/mongo_url instead of an old cached client
    evict_user_db(user_id)

    return StorageTestResult(success=True, message="Storage settings saved")


@router.post("/ai-provider")
async def update_ai_provider_settings(
    body: AIProviderSettingsUpdate,
    current_user: dict = Depends(get_current_user),
    shared_db: AsyncIOMotorDatabase = Depends(get_db),
):
    user_id = str(current_user["_id"])

    update_fields = {
        "ai_provider": body.ai_provider,
        "updated_at": datetime.now(timezone.utc),
    }
    # ai_key is optional (omit to fall back to the platform's default Groq
    # key in ai_service) -- only touch ai_key_encrypted if a key was actually
    # provided, so posting without a key doesn't wipe a previously saved one
    if body.ai_key:
        update_fields["ai_key_encrypted"] = encrypt_value(body.ai_key)

    await shared_db["user_settings"].update_one(
        {"user_id": user_id},
        {"$set": update_fields, "$setOnInsert": {"user_id": user_id}},
        upsert=True,
    )

    return StorageTestResult(success=True, message="AI provider settings saved")


@router.get("", response_model=SettingsResponse)
async def get_settings(
    current_user: dict = Depends(get_current_user),
    shared_db: AsyncIOMotorDatabase = Depends(get_db),
):
    user_id = str(current_user["_id"])

    settings_doc = await shared_db["user_settings"].find_one({"user_id": user_id})
    gmail_doc = await shared_db["gmail_connections"].find_one({"user_id": user_id})

    settings_doc = settings_doc or {}

    # decrypt only in-process to derive the masked display value -- the
    # decrypted raw value itself is never put on the response model
    mongo_url_masked = None
    encrypted_mongo_url = settings_doc.get("mongo_url_encrypted")
    if encrypted_mongo_url:
        try:
            mongo_url_masked = _mask_secret(decrypt_value(encrypted_mongo_url))
        except Exception:
            mongo_url_masked = "••••????"  # decrypt failed (e.g. key rotated) -- still don't error the whole response

    ai_key_masked = None
    encrypted_ai_key = settings_doc.get("ai_key_encrypted")
    if encrypted_ai_key:
        try:
            ai_key_masked = _mask_secret(decrypt_value(encrypted_ai_key))
        except Exception:
            ai_key_masked = "••••????"

    return SettingsResponse(
        storage_mode=settings_doc.get("storage_mode", "shared"),
        mongo_url_masked=mongo_url_masked,
        ai_provider=settings_doc.get("ai_provider", "groq"),
        ai_key_masked=ai_key_masked,
        gmail_connected=gmail_doc is not None,
    )


@router.get("/gmail/connect", response_model=GmailConnectResponse)
async def gmail_connect(
    current_user: dict = Depends(get_current_user),
):
    """Builds the Google OAuth consent URL. state carries the user_id so the
    public /gmail/callback (no JWT available on a browser redirect) knows
    whose gmail_connections row to write to."""
    user_id = str(current_user["_id"])

    params = {
        "client_id": app_settings.GOOGLE_CLIENT_ID,
        "redirect_uri": app_settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "scope": "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/userinfo.email",
        "state": user_id,
    }
    consent_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)

    return GmailConnectResponse(auth_url=consent_url)


@router.get("/gmail/callback")
async def gmail_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    shared_db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Public OAuth redirect target -- Google calls this directly, so there's
    no JWT here. state (set to user_id in gmail_connect) is how we know whose
    connection to save. Always redirects back to the website; success/failure
    is communicated via query params rather than a JSON body since this is a
    browser redirect, not an API call the frontend fetches."""
    website_settings_url = app_settings.WEBSITE_SETTINGS_URL

    if error:
        return RedirectResponse(url=f"{website_settings_url}?gmail_connected=false&reason={error}")

    if not code or not state:
        return RedirectResponse(url=f"{website_settings_url}?gmail_connected=false&reason=missing_code_or_state")

    user_id = state

    try:
        tokens = await exchange_code_for_tokens(code)
        await save_gmail_connection(user_id, tokens, shared_db)
    except Exception:
        # never leak token exchange internals (which could include raw
        # tokens/secrets in exception args) into the redirect URL or logs
        return RedirectResponse(url=f"{website_settings_url}?gmail_connected=false&reason=exchange_failed")

    return RedirectResponse(url=f"{website_settings_url}?gmail_connected=true")
