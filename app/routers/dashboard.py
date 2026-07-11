from fastapi import APIRouter, Depends

from app.core.db import get_user_db
from app.core.security import get_current_user_id

router = APIRouter(tags=["dashboard"])


def _serialize_session(s: dict) -> dict:
    return {
        "id": str(s["_id"]),
        "online": s.get("online", False),
        "last_seen": s.get("last_seen"),
    }


def _serialize_hr_contact(c: dict) -> dict:
    return {
        "id": str(c["_id"]),
        "name": c.get("name"),
        "email": c.get("email"),
        "company": c.get("company"),
        "found_at": c.get("found_at"),
        "source": c.get("source"),
    }


@router.get("/sessions")
async def get_sessions(user_id: str = Depends(get_current_user_id)):
    db = await get_user_db(user_id)
    sessions = await db.bot_sessions.find({"user_id": user_id}).sort("last_seen", -1).to_list(length=50)
    return [_serialize_session(s) for s in sessions]


@router.get("/hr-contacts")
async def get_hr_contacts(user_id: str = Depends(get_current_user_id)):
    db = await get_user_db(user_id)
    contacts = await db.hr_contacts.find({"user_id": user_id}).sort("found_at", -1).to_list(length=500)
    return [_serialize_hr_contact(c) for c in contacts]
