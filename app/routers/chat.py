"""
chat.py — REST endpoints backing the "Chat" page.

The user types plain language ("I need frontend jobs in Hyderabad").
agent_brain.handle_chat_message decides whether that's a job-search request
or just conversation. If it's a job search, this router writes a real
job_requests document — the exact same collection/shape jobs.py's
submit_job_request() already writes to — so nothing downstream (the future
worker, the bot, the dashboard) needs to know or care whether a request
came from the structured job-request form or from chat.

Conversations: each user can have multiple chat threads (ChatGPT-style
sidebar), tracked in the `conversations` collection. `chat_messages` now
carries a `conversation_id` so history/AI context is scoped per-thread
instead of one continuous stream per user. Old rows written before this
change have no conversation_id — get_chat_history/list_conversations treat
that as a single implicit "legacy" thread so nothing 404s for existing
users; a fresh conversation is created the next time they send a message
with no conversation_id given.
"""

from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.db import get_core_db, get_user_db
from app.core.security import get_current_user_id
from app.routers.ws import start_automation_session
from app.services import agent_brain

router = APIRouter(prefix="/chat", tags=["chat"])

HISTORY_LIMIT = 50
TITLE_MAX_LEN = 48


class ChatMessageBody(BaseModel):
    message: str
    conversation_id: str | None = None


def _serialize_message(msg: dict) -> dict:
    return {
        "role": msg["role"],
        "content": msg["content"],
        "created_at": msg["created_at"],
    }


def _serialize_conversation(convo: dict) -> dict:
    return {
        "id": str(convo["_id"]),
        "title": convo.get("title") or "New chat",
        "created_at": convo.get("created_at"),
        "updated_at": convo.get("updated_at"),
    }


def _make_title(text: str) -> str:
    text = " ".join(text.split())  # collapse whitespace/newlines
    if len(text) <= TITLE_MAX_LEN:
        return text or "New chat"
    return text[:TITLE_MAX_LEN].rstrip() + "…"


async def _get_owned_conversation(db, user_id: str, conversation_id: str) -> dict:
    try:
        oid = ObjectId(conversation_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    convo = await db.conversations.find_one({"_id": oid, "user_id": user_id})
    if convo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return convo


@router.get("/conversations")
async def list_conversations(user_id: str = Depends(get_current_user_id)):
    db = await get_user_db(user_id)
    convos = await db.conversations.find({"user_id": user_id}).sort("updated_at", -1).to_list(length=200)
    return [_serialize_conversation(c) for c in convos]


@router.post("/conversations", status_code=status.HTTP_201_CREATED)
async def create_conversation(user_id: str = Depends(get_current_user_id)):
    db = await get_user_db(user_id)
    now = datetime.now(timezone.utc)
    doc = {"user_id": user_id, "title": "New chat", "created_at": now, "updated_at": now}
    result = await db.conversations.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _serialize_conversation(doc)


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, user_id: str = Depends(get_current_user_id)):
    db = await get_user_db(user_id)
    await _get_owned_conversation(db, user_id, conversation_id)
    await db.conversations.delete_one({"_id": ObjectId(conversation_id), "user_id": user_id})
    await db.chat_messages.delete_many({"conversation_id": conversation_id, "user_id": user_id})
    return {"deleted": True}


@router.get("/conversations/{conversation_id}/history")
async def get_conversation_history(conversation_id: str, user_id: str = Depends(get_current_user_id)):
    db = await get_user_db(user_id)
    await _get_owned_conversation(db, user_id, conversation_id)
    msgs = await db.chat_messages.find(
        {"conversation_id": conversation_id, "user_id": user_id}
    ).sort("created_at", 1).to_list(length=HISTORY_LIMIT)
    return [_serialize_message(m) for m in msgs]


@router.get("/history")
async def get_chat_history(user_id: str = Depends(get_current_user_id)):
    """
    Legacy endpoint, kept so nothing already deployed against it breaks.
    Returns messages written before conversation_id existed (conversation_id
    absent/null) — the pre-migration single-thread history.
    """
    db = await get_user_db(user_id)
    msgs = await db.chat_messages.find(
        {"user_id": user_id, "conversation_id": {"$in": [None, ""]}}
    ).sort("created_at", 1).to_list(length=HISTORY_LIMIT)
    return [_serialize_message(m) for m in msgs]


@router.post("")
async def send_chat_message(body: ChatMessageBody, user_id: str = Depends(get_current_user_id)):
    db = await get_user_db(user_id)
    now = datetime.now(timezone.utc)
    text = body.message.strip()

    conversation_id = body.conversation_id
    is_first_message = False

    if conversation_id:
        await _get_owned_conversation(db, user_id, conversation_id)
    else:
        # No thread given — start a fresh one, same as clicking "New chat"
        # would have, so callers that haven't been updated yet (or a first
        # message from a brand-new sidebar session) still work.
        convo_doc = {"user_id": user_id, "title": _make_title(text), "created_at": now, "updated_at": now}
        inserted = await db.conversations.insert_one(convo_doc)
        conversation_id = str(inserted.inserted_id)
        is_first_message = True

    user_msg = {
        "user_id": user_id,
        "conversation_id": conversation_id,
        "role": "user",
        "content": text,
        "created_at": now,
    }
    await db.chat_messages.insert_one(user_msg)

    history_docs = await db.chat_messages.find(
        {"conversation_id": conversation_id, "user_id": user_id}
    ).sort("created_at", 1).to_list(length=HISTORY_LIMIT)
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

        # Same live-kickoff as the structured job-request form (jobs.py) —
        # a chat-originated "find me frontend jobs" should behave exactly
        # like filling out the form, not silently do less because it came
        # through a different entry point.
        started = await start_automation_session(
            user_id, job_request_id, result["job_type"], result["experience_level"], result["target_sites"]
        )
        if started:
            await db.job_requests.update_one({"_id": inserted.inserted_id}, {"$set": {"status": "processing"}})

        core_db = get_core_db()
        await core_db.settings.update_one({"user_id": user_id}, {"$inc": {"applications_count": 1}}, upsert=True)

    reply_now = datetime.now(timezone.utc)
    assistant_msg = {
        "user_id": user_id,
        "conversation_id": conversation_id,
        "role": "assistant",
        "content": result["reply"],
        "created_at": reply_now,
    }
    await db.chat_messages.insert_one(assistant_msg)

    # Bump conversation's updated_at so the sidebar list re-sorts to the top,
    # same as ChatGPT bumping the most recently active thread up.
    update_fields = {"updated_at": reply_now}
    if is_first_message:
        # Title was already set from the user's first message at creation
        # time above — nothing else to set here, just keep updated_at fresh.
        pass
    await db.conversations.update_one({"_id": ObjectId(conversation_id)}, {"$set": update_fields})

    # Account-level usage counter (numbers only) lives in the hosted core DB.
    core_db = get_core_db()
    await core_db.settings.update_one({"user_id": user_id}, {"$inc": {"chats_count": 1}}, upsert=True)

    return {
        "conversation_id": conversation_id,
        "reply": result["reply"],
        "intent": result["intent"],
        "job_request_id": job_request_id,
        "job_type": result.get("job_type"),
        "experience_level": result.get("experience_level"),
        "target_sites": result.get("target_sites"),
        "created_at": reply_now,
    }
