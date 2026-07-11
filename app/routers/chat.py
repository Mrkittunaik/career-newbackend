"""
chat.py — REST endpoint backing the sidebar "Chat" page.

The user types plain language ("I need frontend jobs in Hyderabad").
agent_brain.handle_chat_message decides whether that's a job-search request
or just conversation. If it's a job search, this router writes a real
job_requests document — the exact same collection/shape jobs.py's
submit_job_request() already writes to — so nothing downstream (the future
worker, the bot, the dashboard) needs to know or care whether a request
came from the structured job-request form or from chat.

Chat history itself is persisted per-user so the page can be reopened
without losing context, and so agent_brain has recent turns for follow-up
messages (e.g. "actually make it remote only").
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.db import get_db
from app.core.security import get_current_user_id
from app.services import agent_brain

router = APIRouter(prefix="/chat", tags=["chat"])

HISTORY_LIMIT = 50


class ChatMessageBody(BaseModel):
    message: str


def _serialize(msg: dict) -> dict:
    return {
        "role": msg["role"],
        "content": msg["content"],
        "created_at": msg["created_at"],
    }


@router.get("/history")
async def get_chat_history(user_id: str = Depends(get_current_user_id)):
    db = get_db()
    msgs = await db.chat_messages.find({"user_id": user_id}).sort("created_at", 1).to_list(length=HISTORY_LIMIT)
    return [_serialize(m) for m in msgs]


@router.post("")
async def send_chat_message(body: ChatMessageBody, user_id: str = Depends(get_current_user_id)):
    db = get_db()
    now = datetime.now(timezone.utc)
    text = body.message.strip()

    user_msg = {"user_id": user_id, "role": "user", "content": text, "created_at": now}
    await db.chat_messages.insert_one(user_msg)

    history_docs = await db.chat_messages.find({"user_id": user_id}).sort("created_at", 1).to_list(length=HISTORY_LIMIT)
    history = [{"role": d["role"], "content": d["content"]} for d in history_docs]

    result = await agent_brain.handle_chat_message(user_id, text, history)

    job_request_id = None
    if result["intent"] == "job_search":
        job_doc = {
            "user_id": user_id,
            "job_type": result["job_type"],
            "experience_level": result["experience_level"],
            "target_sites": result["target_sites"],
            "created_at": datetime.now(timezone.utc),
            "status": "queued",
            "source": "chat",
        }
        inserted = await db.job_requests.insert_one(job_doc)
        job_request_id = str(inserted.inserted_id)

    reply_now = datetime.now(timezone.utc)
    assistant_msg = {"user_id": user_id, "role": "assistant", "content": result["reply"], "created_at": reply_now}
    await db.chat_messages.insert_one(assistant_msg)

    return {
        "reply": result["reply"],
        "intent": result["intent"],
        "job_request_id": job_request_id,
        "job_type": result.get("job_type"),
        "experience_level": result.get("experience_level"),
        "target_sites": result.get("target_sites"),
        "created_at": reply_now,
    }
