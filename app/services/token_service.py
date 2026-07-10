from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.security import generate_bot_token as _generate_raw_token
from app.core.security import hash_bot_token
from app.models.bot_token import BotToken


async def generate_bot_token(user_id: str, db: AsyncIOMotorDatabase) -> str:
    """
    Issues a fresh bot token for a user, works identically whether this is the
    user's first pairing or a regeneration of an existing token.

    Steps (kept sequential rather than a true multi-doc transaction, since a
    standalone Mongo deployment may not have a replica set to support one --
    the collection only ever holds one active token per user, and stale/old
    tokens being briefly deletable-but-not-yet-replaced is an acceptable
    window since the old raw token is unrecoverable/unusable without the hash
    matching anyway):
      1. Delete any existing bot_tokens row(s) for this user (covers both the
         "revoke old" path and the "no row yet" first-time-pairing path --
         delete_many on zero matches is a no-op).
      2. Generate a new raw token + its hash.
      3. Insert the new bot_tokens row.
      4. Return the RAW token -- this is the only time it's ever available;
         only the hash is persisted.
    """
    await db["bot_tokens"].delete_many({"user_id": user_id})

    raw_token = _generate_raw_token()
    token_hash = hash_bot_token(raw_token)

    bot_token_doc = BotToken(user_id=user_id, token_hash=token_hash)
    await db["bot_tokens"].insert_one(bot_token_doc.to_mongo())

    return raw_token


async def validate_bot_token(raw_token: str, db: AsyncIOMotorDatabase) -> Optional[str]:
    """
    Hashes the incoming raw token and looks up a matching, non-revoked
    bot_tokens row. Returns the owning user_id as a string, or None if the
    token is invalid/revoked/doesn't exist.
    """
    token_hash = hash_bot_token(raw_token)

    token_doc = await db["bot_tokens"].find_one(
        {"token_hash": token_hash, "revoked_at": None}
    )
    if not token_doc:
        return None

    return str(token_doc["user_id"])
