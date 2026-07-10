"""
Internal-only endpoints used by the worker process (worker.py), never called
by the frontend. Protected by a shared secret (INTERNAL_API_SECRET), not by
user JWTs, since the worker acts on behalf of many users at once.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from app.core.config import settings
from app.core.db import get_db
from app.routers.ws import manager

router = APIRouter(prefix="/internal", tags=["internal"])


def _check_secret(x_internal_secret: str | None):
    if not x_internal_secret or x_internal_secret != settings.internal_api_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid internal secret")


class ApplicationUpsert(BaseModel):
    user_id: str
    role: str
    company: str
    site: str
    status: str  # submitted | skipped | failed | needs_attention | pending
    link: str | None = None
    reply_received: bool = False
    reply_snippet: str | None = None


class HrContactCreate(BaseModel):
    user_id: str
    name: str
    email: str
    company: str
    source: str


class BotStatusUpdate(BaseModel):
    user_id: str
    online: bool


class PushEventBody(BaseModel):
    user_id: str
    event_type: str
    payload: dict


@router.post("/job-requests/{request_id}/claim")
async def claim_next_job_request(request_id: str, x_internal_secret: str | None = Header(default=None)):
    """Worker polls this to atomically grab the next queued job request."""
    _check_secret(x_internal_secret)
    db = get_db()
    from bson import ObjectId

    doc = await db.job_requests.find_one_and_update(
        {"_id": ObjectId(request_id), "status": "queued"},
        {"$set": {"status": "processing", "started_at": datetime.now(timezone.utc)}},
    )
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found or already claimed")
    doc["_id"] = str(doc["_id"])
    return doc


@router.get("/job-requests/queued")
async def list_queued_job_requests(x_internal_secret: str | None = Header(default=None)):
    _check_secret(x_internal_secret)
    db = get_db()
    docs = await db.job_requests.find({"status": "queued"}).to_list(length=100)
    for d in docs:
        d["_id"] = str(d["_id"])
    return docs


@router.post("/job-requests/{request_id}/complete")
async def complete_job_request(request_id: str, x_internal_secret: str | None = Header(default=None)):
    _check_secret(x_internal_secret)
    db = get_db()
    from bson import ObjectId

    await db.job_requests.update_one(
        {"_id": ObjectId(request_id)},
        {"$set": {"status": "completed", "completed_at": datetime.now(timezone.utc)}},
    )
    return {"completed": True}


@router.post("/applications")
async def upsert_application(body: ApplicationUpsert, x_internal_secret: str | None = Header(default=None)):
    _check_secret(x_internal_secret)
    db = get_db()
    doc = {
        "user_id": body.user_id,
        "role": body.role,
        "company": body.company,
        "site": body.site,
        "status": body.status,
        "link": body.link,
        "reply_received": body.reply_received,
        "reply_snippet": body.reply_snippet,
        "applied_at": datetime.now(timezone.utc),
    }
    result = await db.job_applications.insert_one(doc)
    doc["id"] = str(result.inserted_id)

    await manager.send_to_user(
        body.user_id,
        "job_progress_update",
        {
            "id": doc["id"],
            "role": doc["role"],
            "company": doc["company"],
            "site": doc["site"],
            "status": doc["status"],
            "link": doc["link"],
            "reply_received": doc["reply_received"],
            "applied_at": doc["applied_at"].isoformat(),
        },
    )
    return {"id": doc["id"]}


@router.post("/hr-contacts")
async def create_hr_contact(body: HrContactCreate, x_internal_secret: str | None = Header(default=None)):
    _check_secret(x_internal_secret)
    db = get_db()
    doc = {
        "user_id": body.user_id,
        "name": body.name,
        "email": body.email,
        "company": body.company,
        "source": body.source,
        "found_at": datetime.now(timezone.utc),
    }
    result = await db.hr_contacts.insert_one(doc)
    doc_id = str(result.inserted_id)

    await manager.send_to_user(
        body.user_id,
        "hr_contact_added",
        {
            "id": doc_id,
            "name": doc["name"],
            "email": doc["email"],
            "company": doc["company"],
            "found_at": doc["found_at"].isoformat(),
        },
    )
    return {"id": doc_id}


@router.post("/bot-status")
async def update_bot_status(body: BotStatusUpdate, x_internal_secret: str | None = Header(default=None)):
    _check_secret(x_internal_secret)
    db = get_db()
    now = datetime.now(timezone.utc)
    await db.bot_sessions.update_one(
        {"user_id": body.user_id},
        {"$set": {"online": body.online, "last_seen": now}},
        upsert=True,
    )
    await db.settings.update_one({"user_id": body.user_id}, {"$set": {"bot_online": body.online}}, upsert=True)

    await manager.send_to_user(body.user_id, "bot_status", {"online": body.online, "last_seen": now.isoformat()})
    return {"ok": True}


@router.post("/push-event")
async def push_event(body: PushEventBody, x_internal_secret: str | None = Header(default=None)):
    """Generic escape hatch for any event type not covered by the endpoints above."""
    _check_secret(x_internal_secret)
    await manager.send_to_user(body.user_id, body.event_type, body.payload)
    return {"ok": True}
