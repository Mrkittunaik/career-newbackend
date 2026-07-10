from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings

_client: AsyncIOMotorClient | None = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(settings.mongo_url)
    return _client


def get_db():
    """Returns the Motor database handle. Call db.<collection> to use it."""
    return get_client()[settings.mongo_db_name]


async def ensure_indexes():
    """Creates all indexes the app relies on. Called once at startup."""
    db = get_db()
    await db.users.create_index("email", unique=True)
    await db.profiles.create_index("user_id", unique=True)
    await db.documents.create_index("user_id")
    await db.job_applications.create_index("user_id")
    await db.job_applications.create_index([("user_id", 1), ("status", 1)])
    await db.hr_contacts.create_index("user_id")
    await db.bot_sessions.create_index("user_id")
    await db.settings.create_index("user_id", unique=True)
    await db.payments.create_index("order_id", unique=True)
    await db.job_requests.create_index("user_id")
