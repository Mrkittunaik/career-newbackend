from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.core.config import settings
from app.core.security import encrypt_value
from app.models.gmail_connection import GmailConnection, OAuthTokens

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


class RawGoogleTokens(BaseModel):
    """Shape of Google's token endpoint response. Kept private to this
    module -- callers only ever see the already-encrypted OAuthTokens."""
    access_token: str
    refresh_token: Optional[str] = None
    expires_in: Optional[int] = None
    scope: Optional[str] = None
    token_type: Optional[str] = "Bearer"


async def exchange_code_for_tokens(code: str) -> RawGoogleTokens:
    """Exchanges the OAuth authorization code for access/refresh tokens.
    Raises on any non-2xx response or malformed body -- caller (the
    /settings/gmail/callback route) is responsible for turning that into a
    safe, non-leaky redirect."""
    payload = {
        "code": code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(GOOGLE_TOKEN_URL, data=payload)
        response.raise_for_status()
        return RawGoogleTokens.model_validate(response.json())


async def save_gmail_connection(
    user_id: str,
    tokens: RawGoogleTokens,
    shared_db: AsyncIOMotorDatabase,
) -> None:
    """Encrypts both tokens before persisting. Upserts so a re-connect
    (e.g. re-consenting after a revoked refresh token) replaces the old row
    rather than accumulating stale connections for the same user."""
    expiry: Optional[datetime] = None
    if tokens.expires_in is not None:
        expiry = datetime.now(timezone.utc) + timedelta(seconds=tokens.expires_in)

    oauth_tokens = OAuthTokens(
        access_token_encrypted=encrypt_value(tokens.access_token),
        refresh_token_encrypted=encrypt_value(tokens.refresh_token) if tokens.refresh_token else None,
        expiry=expiry,
        scope=tokens.scope,
        token_type=tokens.token_type or "Bearer",
    )

    connection = GmailConnection(user_id=user_id, oauth_tokens=oauth_tokens)
    connection_doc = connection.to_mongo()

    await shared_db["gmail_connections"].update_one(
        {"user_id": user_id},
        {"$set": connection_doc},
        upsert=True,
    )
