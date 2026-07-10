from datetime import datetime, time, timezone
from typing import List, Optional

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

from app.core.config import settings
from app.models.job_queue import JobQueue
from app.models.job_request import JobRequest
from app.services.matching_service import build_search_query


def _today_range_utc() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    end = datetime.combine(now.date(), time.max, tzinfo=timezone.utc)
    return start, end


async def _get_daily_job_limit(user_id: str, shared_db: AsyncIOMotorDatabase) -> int:
    """
    Daily limit comes off the user's plan if set, otherwise falls back to
    settings.DEFAULT_DAILY_JOB_LIMIT. (Master spec lists `plans` as either its
    own collection or a field directly on users -- we go with a field on the
    user doc here since no plans model/routes have been built yet; swap this
    lookup out if a dedicated plans collection gets built later.)
    """
    user = await shared_db["users"].find_one({"_id": ObjectId(user_id)})
    if user and user.get("daily_job_limit"):
        return int(user["daily_job_limit"])
    return settings.DEFAULT_DAILY_JOB_LIMIT


async def check_daily_limit_remaining(
    user_id: str,
    shared_db: AsyncIOMotorDatabase,
    user_db: AsyncIOMotorDatabase,
) -> int:
    """
    Remaining slots for today, computed by COUNTING today's submitted
    job_applications (not a separately maintained/manually-reset counter --
    avoids drift per the master spec's business rules).
    """
    limit = await _get_daily_job_limit(user_id, shared_db)
    start, end = _today_range_utc()

    applied_today = await user_db["job_applications"].count_documents(
        {
            "user_id": user_id,
            "status": "submitted",
            "applied_at": {"$gte": start, "$lte": end},
        }
    )

    return max(limit - applied_today, 0)


async def get_daily_counter_stats(
    user_id: str,
    shared_db: AsyncIOMotorDatabase,
    user_db: AsyncIOMotorDatabase,
) -> dict:
    """applied_today + limit, for the /ws/dashboard daily_counter_update push.
    Separate from check_daily_limit_remaining() since that only returns the
    remaining count and callers pushing to the dashboard need both raw numbers."""
    limit = await _get_daily_job_limit(user_id, shared_db)
    start, end = _today_range_utc()

    applied_today = await user_db["job_applications"].count_documents(
        {
            "user_id": user_id,
            "status": "submitted",
            "applied_at": {"$gte": start, "$lte": end},
        }
    )

    return {"applied_today": applied_today, "limit": limit}


async def create_job_request(
    user_id: str,
    job_type: str,
    experience_level: str,
    target_sites: List[str],
    shared_db: AsyncIOMotorDatabase,
    user_db: AsyncIOMotorDatabase,
) -> dict:
    """
    Creates the job_request doc + one job_queue entry per target_site (each
    with an AI-built search_query), status="pending".

    We still create the request+queue even if today's limit is already used
    up -- the limit is enforced per-task at send time (get_next_task /
    check_daily_limit_remaining), not at request-creation time, since a
    request made late today should still queue up and start being worked the
    moment the limit resets tomorrow. Callers that want to hard-block request
    creation when the limit is exhausted can check
    check_daily_limit_remaining() themselves before calling this.
    """
    job_request = JobRequest(
        user_id=user_id,
        job_type=job_type,
        experience_level=experience_level,
        target_sites=target_sites,
        status="pending",
    )
    result = await user_db["job_requests"].insert_one(job_request.to_mongo())
    job_request_id = str(result.inserted_id)

    queue_entries = []
    for site in target_sites:
        search_query = await build_search_query(job_type, experience_level, site, user_id, shared_db)
        queue_item = JobQueue(
            job_request_id=job_request_id,
            site=site,
            search_query=search_query,
            status="pending",
        )
        queue_entries.append(queue_item.to_mongo())

    if queue_entries:
        await user_db["job_queue"].insert_many(queue_entries)

    job_request_doc = await user_db["job_requests"].find_one({"_id": result.inserted_id})
    return job_request_doc


async def get_next_task(
    user_id: str,
    shared_db: AsyncIOMotorDatabase,
    user_db: AsyncIOMotorDatabase,
) -> Optional[dict]:
    """
    Pulls the next pending job_queue item belonging to this user (via their
    job_requests) for their active bot session, marks it "sent", and returns
    it -- this is what gets wrapped into a task_assigned WS event.

    Enforces the daily limit rule here (check before assigning EACH new
    task): returns None if today's allowance is already used up, so the
    caller (the /ws/autoapply handler) can send daily_limit_reached instead
    of task_assigned.
    """
    remaining = await check_daily_limit_remaining(user_id, shared_db, user_db)
    if remaining <= 0:
        return None

    active_session = await user_db["bot_sessions"].find_one(
        {"user_id": user_id, "status": "running"}
    )
    if not active_session:
        return None

    user_job_requests = await user_db["job_requests"].find(
        {"user_id": user_id, "status": {"$in": ["pending", "active"]}}
    ).to_list(length=None)
    job_request_ids = [str(jr["_id"]) for jr in user_job_requests]

    if not job_request_ids:
        return None

    task_doc = await user_db["job_queue"].find_one_and_update(
        {"job_request_id": {"$in": job_request_ids}, "status": "pending"},
        {"$set": {"status": "sent", "updated_at": datetime.now(timezone.utc)}},
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )

    return task_doc
