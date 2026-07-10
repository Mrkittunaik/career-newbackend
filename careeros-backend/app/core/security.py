import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.core.db import get_db, get_shared_db

bearer_scheme = HTTPBearer(auto_error=False)

# =========================================================
# JWT (website user auth)
# =========================================================

def create_access_token(user_id: str, extra_claims: Optional[dict] = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


# =========================================================
# Bot token (Electron bot auth)
# One active token per user. We store only the hash; raw token
# is shown to the user exactly once (at generation time).
# sha256 is used (not bcrypt) since the token itself is a high-entropy
# random secret, not a low-entropy user password -- a fast deterministic
# hash lets us look it up directly by hash in bot_tokens instead of
# iterating every row to compare.
# =========================================================

def generate_bot_token() -> str:
    return secrets.token_urlsafe(32)


def hash_bot_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def verify_bot_token(raw_token: str, token_hash: str) -> bool:
    return secrets.compare_digest(hash_bot_token(raw_token), token_hash)


# =========================================================
# Symmetric encryption (mongo_url_encrypted, ai_key_encrypted)
# Two-way, since these need to be decrypted for actual use
# (unlike bot tokens, which are one-way hashed for verification only).
# =========================================================

_fernet = Fernet(settings.ENCRYPTION_KEY.encode("utf-8"))


def encrypt_value(raw_value: str) -> str:
    return _fernet.encrypt(raw_value.encode("utf-8")).decode("utf-8")


def decrypt_value(encrypted_value: str) -> str:
    return _fernet.decrypt(encrypted_value.encode("utf-8")).decode("utf-8")


# =========================================================
# FastAPI dependencies
# =========================================================

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db=Depends(get_db),
) -> dict:
    """Validates website-user JWT (Authorization: Bearer <jwt>), returns the user doc."""
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing credentials")

    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    from bson import ObjectId  # local import keeps bson usage isolated to this lookup

    user = await db["users"].find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    return user


async def get_current_bot_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> dict:
    """Validates bot-token (Authorization: Bearer <bot_token>) for bot-facing REST/WS,
    returns the owning user doc. Rejects revoked tokens."""
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bot token")

    raw_token = credentials.credentials
    token_hash = hash_bot_token(raw_token)

    db = get_shared_db()  # bot_tokens always live in the shared db
    token_doc = await db["bot_tokens"].find_one({"token_hash": token_hash, "revoked_at": None})
    if not token_doc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked bot token")

    from bson import ObjectId

    user = await db["users"].find_one({"_id": ObjectId(token_doc["user_id"])})
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    return user
