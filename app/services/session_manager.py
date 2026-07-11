"""
session_manager.py — persists the state of an in-progress automation run
(one job_request being worked through one or more sites) to Mongo, step by
step, so a dropped connection or bot restart can RESUME instead of losing
all progress and starting the whole job search over.

This directly closes the recovery gap called out earlier: previously
nothing survived a disconnect except the job_applications rows already
written. A job that was mid-scan when the socket died just vanished with
no record of where it was. Now every meaningful step (site opened, filters
applied, job being decided, fields being filled) is written to
automation_sessions immediately, so on reconnect ws.py can look up the
user's last non-terminal session and pick up from that exact step instead
of the bot starting the whole request from the first site again.

STATUS values:
  in_progress  — actively running on a live /ws/bot connection right now
  interrupted  — the bot disconnected mid-session; resumable on reconnect
  completed    — every target site was worked through
  failed       — given up (e.g. no AI key, or explicitly cancelled)

STEP values (what the session was doing when last saved):
  opening_site | awaiting_filters | scanning | awaiting_decision |
  filling | awaiting_next_page
"""

from datetime import datetime, timezone

from bson import ObjectId

from app.core.db import get_user_db


async def start_session(
    user_id: str,
    job_request_id: str,
    job_type: str,
    experience_level: str,
    target_sites: list[str],
) -> dict:
    db = await get_user_db(user_id)
    now = datetime.now(timezone.utc)
    doc = {
        "user_id": user_id,
        "job_request_id": job_request_id,
        "job_type": job_type,
        "experience_level": experience_level,
        "target_sites": target_sites,
        "site_index": 0,
        "status": "in_progress",
        "step": "opening_site",
        "current_url": None,
        "current_job": None,
        "jobs_applied": 0,
        "jobs_skipped": 0,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.automation_sessions.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def get_resumable_session(user_id: str) -> dict | None:
    """Called right after a bot reconnects — finds the most recent
    session that was cut off mid-run (in_progress means the connection
    died without a clean stop; interrupted is the same thing tagged
    explicitly by mark_interrupted). Either state means there's unfinished
    work to hand back to the bot."""
    db = await get_user_db(user_id)
    return await db.automation_sessions.find_one(
        {"user_id": user_id, "status": {"$in": ["in_progress", "interrupted"]}},
        sort=[("updated_at", -1)],
    )


async def update_session(user_id: str, session_id: str, **fields) -> None:
    db = await get_user_db(user_id)
    fields["updated_at"] = datetime.now(timezone.utc)
    await db.automation_sessions.update_one({"_id": ObjectId(session_id)}, {"$set": fields})


async def increment_counter(user_id: str, session_id: str, field_name: str) -> None:
    db = await get_user_db(user_id)
    await db.automation_sessions.update_one(
        {"_id": ObjectId(session_id)},
        {"$inc": {field_name: 1}, "$set": {"updated_at": datetime.now(timezone.utc)}},
    )


async def mark_interrupted(user_id: str) -> None:
    """Called from ws.py's disconnect handler — flags any session still
    marked in_progress for this user as interrupted rather than leaving it
    silently stuck at in_progress forever with no live connection actually
    working it. Doesn't touch already-completed/failed sessions."""
    db = await get_user_db(user_id)
    await db.automation_sessions.update_many(
        {"user_id": user_id, "status": "in_progress"},
        {"$set": {"status": "interrupted", "updated_at": datetime.now(timezone.utc)}},
    )


async def complete_session(user_id: str, session_id: str) -> None:
    await update_session(user_id, session_id, status="completed", step="done")


async def fail_session(user_id: str, session_id: str, reason: str) -> None:
    await update_session(user_id, session_id, status="failed", step="failed", fail_reason=reason)
