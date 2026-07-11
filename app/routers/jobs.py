from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.db import get_user_db, get_core_db
from app.core.security import get_current_user_id

router = APIRouter(prefix="/jobs", tags=["jobs"])

DAILY_APPLICATION_LIMIT = 25


class JobRequestBody(BaseModel):
    job_type: str
    experience_level: str
    target_sites: list[str]


def _serialize_application(app: dict) -> dict:
    return {
        "id": str(app["_id"]),
        "role": app.get("role"),
        "company": app.get("company"),
        "site": app.get("site"),
        "status": app.get("status", "pending"),
        "applied_at": app.get("applied_at"),
        "link": app.get("link"),
        "reply_received": app.get("reply_received", False),
        "reply_snippet": app.get("reply_snippet"),
    }


@router.post("/request", status_code=201)
async def submit_job_request(body: JobRequestBody, user_id: str = Depends(get_current_user_id)):
    db = await get_user_db(user_id)
    doc = {
        "user_id": user_id,
        "job_type": body.job_type,
        "experience_level": body.experience_level,
        "target_sites": body.target_sites,
        "created_at": datetime.now(timezone.utc),
        "status": "queued",
    }
    result = await db.job_requests.insert_one(doc)
    # NOTE: the actual scanning/auto-apply bot is a separate worker process that should
    # watch the `job_requests` collection (status == "queued") and write results into
    # `job_applications` + push WebSocket events. This endpoint only enqueues the request.

    # Account-level usage counter (numbers only) lives in the hosted core DB
    # regardless of storage_mode — see db.py's split rationale.
    core_db = get_core_db()
    await core_db.settings.update_one({"user_id": user_id}, {"$inc": {"applications_count": 1}}, upsert=True)

    return {"id": str(result.inserted_id), "status": "queued"}


@router.get("")
async def get_job_applications(
    status_: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None),
    user_id: str = Depends(get_current_user_id),
):
    db = await get_user_db(user_id)
    query: dict = {"user_id": user_id}
    if status_ and status_ != "all":
        query["status"] = status_
    if search:
        query["$or"] = [
            {"role": {"$regex": search, "$options": "i"}},
            {"company": {"$regex": search, "$options": "i"}},
        ]

    apps = await db.job_applications.find(query).sort("applied_at", -1).to_list(length=1000)
    return [_serialize_application(a) for a in apps]


@router.get("/limit")
async def get_daily_limit(user_id: str = Depends(get_current_user_id)):
    db = await get_user_db(user_id)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    applied_today = await db.job_applications.count_documents(
        {"user_id": user_id, "status": "submitted", "applied_at": {"$gte": today_start}}
    )
    return {"applied_today": applied_today, "daily_limit": DAILY_APPLICATION_LIMIT}
