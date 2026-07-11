"""
Symmetric field-level encryption for secrets stored at rest (e.g. a user's
own AI provider API key in settings.ai_api_key). Uses Fernet (AES-128-CBC +
HMAC) via the `cryptography` package, which is already a transitive dep of
python-jose[cryptography].

Requires FIELD_ENCRYPTION_KEY to be set in the environment for production
use. If unset, falls back to a dev-only in-memory key so local development
doesn't hard-crash — but this means encrypted values won't survive a
restart in dev, which is fine since it's not persistent test data anyway.
"""

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_dev_fallback_key = Fernet.generate_key()


def _get_fernet() -> Fernet:
    key = settings.field_encryption_key.strip() or _dev_fallback_key
    if isinstance(key, str):
        key = key.encode()
    return Fernet(key)


def encrypt_secret(plain_text: str) -> str:
    return _get_fernet().encrypt(plain_text.encode()).decode()


def decrypt_secret(encrypted_text: str) -> str | None:
    try:
        return _get_fernet().decrypt(encrypted_text.encode()).decode()
    except (InvalidToken, ValueError):
        return None
