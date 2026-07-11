"""
db.py — two explicit database accessors, not one.

get_core_db() -> always CareerOS's own hosted MongoDB. This is where
account essentials live: users (login/password/account type) and settings
(including usage counters like applications_count/chats_count — numbers
only, not the actual job/chat content). This must always be reachable
regardless of what any individual user has configured, since login and
billing depend on it.

get_user_db(user_id) -> the database that user's own job-hunt CONTENT lives
in: profiles, documents, job_requests, job_applications, job_decisions,
hr_contacts, chat_messages. If the user has settings.storage_mode == "own"
with a working mongo_url, this returns a client connected to *their*
cluster. Otherwise (default) it returns the same hosted database as
get_core_db() — so "hosted" mode isn't a special case, it's just "their
user DB happens to be ours."

There is deliberately no bare get_db() anymore: every call site in the
codebase must say which one it means, so a future new feature can't
accidentally default to the wrong database and leak one user's job/chat
data into the shared hosted instance when they explicitly opted into their
own MongoDB.
"""

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.core.config import settings
from app.core.field_encryption import decrypt_secret

_core_client: AsyncIOMotorClient | None = None

# Per-user Mongo clients for "own" storage_mode, keyed by the (encrypted)
# mongo_url string so the same URL reuses one pooled client instead of
# opening a fresh TCP connection on every request. Small in-memory cache;
# fine at CareerOS's current scale. If a user's own cluster becomes
# unreachable, callers fall back to the hosted DB rather than hard-failing
# (see get_user_db below) — a job/chat feature going temporarily read-only
# on the hosted DB is much better than the whole request 500ing.
_user_clients: dict[str, AsyncIOMotorClient] = {}


def get_core_client() -> AsyncIOMotorClient:
    global _core_client
    if _core_client is None:
        _core_client = AsyncIOMotorClient(settings.mongo_url)
    return _core_client


def get_core_db() -> AsyncIOMotorDatabase:
    """
    Account essentials only: users, settings. Always CareerOS's hosted
    MongoDB — never redirected to a user's own database.
    """
    return get_core_client()[settings.mongo_db_name]


async def get_user_db(user_id: str) -> AsyncIOMotorDatabase:
    """
    Job-hunt content database for this specific user: profiles, documents,
    job_requests, job_applications, job_decisions, hr_contacts,
    chat_messages. Reads the user's storage_mode from the CORE db (settings
    collection lives there, not in the user DB itself — a chicken/egg
    otherwise) and connects to their own Mongo if they've opted in.
    """
    core_db = get_core_db()
    doc = await core_db.settings.find_one({"user_id": user_id}) or {}

    if doc.get("storage_mode") != "own" or not doc.get("mongo_url_encrypted"):
        return get_core_db()

    decrypted_url = decrypt_secret(doc["mongo_url_encrypted"])
    if not decrypted_url:
        # Corrupt/undecryptable URL (e.g. FIELD_ENCRYPTION_KEY rotated) —
        # fall back to hosted rather than hard-failing every request that
        # touches job/chat data for this user.
        return get_core_db()

    if decrypted_url not in _user_clients:
        try:
            client = AsyncIOMotorClient(decrypted_url, serverSelectionTimeoutMS=5000)
            _user_clients[decrypted_url] = client
        except Exception:
            return get_core_db()

    client = _user_clients[decrypted_url]
    db_name = doc.get("mongo_db_name") or settings.mongo_db_name
    try:
        # Cheap round-trip to fail fast if their cluster is unreachable,
        # rather than letting the actual query time out deep in a request.
        await client.admin.command("ping")
    except Exception:
        return get_core_db()

    return client[db_name]


async def ensure_indexes():
    """Creates all indexes the app relies on. Called once at startup, against the hosted DB only."""
    db = get_core_db()
    await db.users.create_index("email", unique=True)
    await db.settings.create_index("user_id", unique=True)
    await db.payments.create_index("order_id", unique=True)

    # These collections normally live in each user's own DB via
    # get_user_db(), but indexes are also created here for the hosted-DB
    # fallback path (default "hosted" storage_mode, or when a user's own
    # cluster is unreachable and requests fall back to hosted).
    await db.profiles.create_index("user_id", unique=True)
    await db.documents.create_index("user_id")
    await db.job_applications.create_index("user_id")
    await db.job_applications.create_index([("user_id", 1), ("status", 1)])
    await db.hr_contacts.create_index("user_id")
    await db.bot_sessions.create_index("user_id")
    await db.job_requests.create_index("user_id")
    await db.job_decisions.create_index("user_id")
    await db.automation_sessions.create_index("user_id")
    await db.automation_sessions.create_index([("user_id", 1), ("status", 1), ("updated_at", -1)])
    await db.chat_messages.create_index([("user_id", 1), ("created_at", 1)])
    await db.chat_messages.create_index([("conversation_id", 1), ("created_at", 1)])
    await db.conversations.create_index([("user_id", 1), ("updated_at", -1)])
