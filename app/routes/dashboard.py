from typing import List

from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.db import get_db, get_user_db
from app.core.security import get_current_user
from app.schemas.dashboard import HRContactResponse, SessionResponse

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


async def _resolve_user_db(user_id: str, shared_db: AsyncIOMotorDatabase) -> AsyncIOMotorDatabase:
    """Same pattern used in routes/jobs.py -- loads this user's settings and
    resolves the correct DB handle (shared vs their own mongo) per storage_mode."""
    settings_doc = await shared_db["user_settings"].find_one({"user_id": user_id})
    settings_dict = settings_doc or {}
    settings_dict["user_id"] = user_id  # ensure present even if no settings row yet
    return await get_user_db(settings_dict)


@router.get("/sessions", response_model=List[SessionResponse])
async def list_sessions(
    limit: int = Query(20, ge=1, le=100),
    skip: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
    shared_db: AsyncIOMotorDatabase = Depends(get_db),
):
    user_id = str(current_user["_id"])
    user_db = await _resolve_user_db(user_id, shared_db)

    cursor = (
        user_db["bot_sessions"]
        .find({"user_id": user_id})
        .sort("started_at", -1)
        .skip(skip)
        .limit(limit)
    )
    sessions = await cursor.to_list(length=None)

    return [
        SessionResponse(
            id=str(session["_id"]),
            user_id=session["user_id"],
            status=session["status"],
            started_at=session["started_at"],
            ended_at=session.get("ended_at"),
        )
        for session in sessions
    ]


@router.get("/hr-contacts", response_model=List[HRContactResponse])
async def list_hr_contacts(
    limit: int = Query(20, ge=1, le=100),
    skip: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
    shared_db: AsyncIOMotorDatabase = Depends(get_db),
):
    user_id = str(current_user["_id"])
    user_db = await _resolve_user_db(user_id, shared_db)

    cursor = (
        user_db["hr_contacts"]
        .find({"user_id": user_id})
        .sort("found_at", -1)
        .skip(skip)
        .limit(limit)
    )
    contacts = await cursor.to_list(length=None)

    return [
        HRContactResponse(
            id=str(contact["_id"]),
            user_id=contact["user_id"],
            session_id=contact["session_id"],
            email=contact["email"],
            company=contact["company"],
            source=contact["source"],
            found_at=contact["found_at"],
        )
        for contact in contacts
    ]
