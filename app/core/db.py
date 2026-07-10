from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.core.config import settings

# ---- shared (default) client ----
_shared_client: AsyncIOMotorClient = AsyncIOMotorClient(settings.MONGO_URL)
_shared_db: AsyncIOMotorDatabase = _shared_client[settings.DB_NAME]

# ---- cache of per-user "own mongo" clients ----
# key: user_id (str) -> (AsyncIOMotorClient, AsyncIOMotorDatabase)
_user_db_cache: dict[str, tuple[AsyncIOMotorClient, AsyncIOMotorDatabase]] = {}


def get_shared_db() -> AsyncIOMotorDatabase:
    return _shared_db


async def get_db() -> AsyncIOMotorDatabase:
    """FastAPI dependency for routes/services that always use the shared DB
    (e.g. users, bot_tokens, user_settings themselves)."""
    return _shared_db


async def get_user_db(user_settings: dict) -> AsyncIOMotorDatabase:
    """
    Returns the correct DB handle for a user's session/application/hr_contact writes+reads.

    - storage_mode == "shared" (or missing) -> shared db.
    - storage_mode == "own" -> user's own mongo cluster, decrypted from
      user_settings["mongo_url_encrypted"]. Connections are cached per user_id
      so we don't reconnect on every request.
    """
    storage_mode = user_settings.get("storage_mode", "shared")

    if storage_mode != "own":
        return _shared_db

    user_id = str(user_settings.get("user_id"))
    encrypted_url = user_settings.get("mongo_url_encrypted")

    if not user_id or not encrypted_url:
        # misconfigured "own" mode -> fail safe to shared db
        return _shared_db

    if user_id in _user_db_cache:
        return _user_db_cache[user_id][1]

    # lazy import to avoid circular import (security.py -> db.py for get_current_user)
    from app.core.security import decrypt_value

    mongo_url = decrypt_value(encrypted_url)
    client: AsyncIOMotorClient = AsyncIOMotorClient(mongo_url)
    db: AsyncIOMotorDatabase = client[settings.DB_NAME]

    _user_db_cache[user_id] = (client, db)
    return db


def evict_user_db(user_id: str) -> None:
    """Call this if a user updates/rotates their own mongo_url, so the next
    get_user_db() call reconnects instead of using a stale cached client."""
    entry = _user_db_cache.pop(str(user_id), None)
    if entry:
        entry[0].close()


async def close_all_connections() -> None:
    """Call on app shutdown."""
    _shared_client.close()
    for client, _ in _user_db_cache.values():
        client.close()
