from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.db import get_shared_db, get_user_db
from app.models.bot_session import BotSession
from app.models.hr_contact import HRContact
from app.schemas.ws_events import (
    AnswerGeneratedEvent,
    ApplicationFailedEvent,
    ApplicationSkippedEvent,
    ApplicationSubmittedEvent,
    ApplyJobItem,
    ApplyToEvent,
    DailyLimitReachedEvent,
    FileReadyEvent,
    FileRequestedEvent,
    HrEmailFoundEvent,
    JobsFoundEvent,
    QuestionAskedEvent,
    SessionStateEvent,
    StartSessionEvent,
    StopSessionEvent,
    TaskAssignedEvent,
    TokenInvalidEvent,
    parse_autoapply_message,
)
from app.services.ai_service import generate_answer
from app.services.file_service import get_document_for_field
from app.services.matching_service import filter_matching_jobs
from app.services.queue_service import (
    check_daily_limit_remaining,
    get_daily_counter_stats,
    get_next_task,
)
from app.services.token_service import validate_bot_token
from app.ws.autoapply_manager import manager
from app.ws.dashboard_manager import manager as dashboard_manager

router = APIRouter()


async def _resolve_user_db(user_id: str, shared_db):
    """Same pattern used in routes/jobs.py -- loads user_settings and resolves
    shared vs own-mongo per storage_mode."""
    settings_doc = await shared_db["user_settings"].find_one({"user_id": user_id})
    settings_dict = settings_doc or {}
    settings_dict["user_id"] = user_id
    return await get_user_db(settings_dict)


async def _push_dashboard_update(user_id: str, event: dict) -> None:
    # push-only, best-effort: if the user doesn't have their dashboard open,
    # push_to_user just returns False and there's nothing further to do here
    await dashboard_manager.push_to_user(user_id, event)


async def _send_task_or_completion(websocket: WebSocket, user_id: str, shared_db, user_db, session_id: str) -> None:
    """Shared by start_session and the post-application-result flow: pulls
    the next task and sends either task_assigned or session_state/daily_limit_reached."""
    task_doc = await get_next_task(user_id, shared_db, user_db)

    if task_doc is not None:
        await websocket.send_json(
            TaskAssignedEvent(
                task_id=str(task_doc["_id"]),
                site=task_doc["site"],
                search_query=task_doc["search_query"],
            ).model_dump()
        )
        return

    remaining = await check_daily_limit_remaining(user_id, shared_db, user_db)
    if remaining <= 0:
        await websocket.send_json(DailyLimitReachedEvent().model_dump())
    else:
        # no task available but limit isn't the reason -- treat as completed
        # (nothing left queued for this session)
        await websocket.send_json(
            SessionStateEvent(session_id=session_id, status="completed").model_dump()
        )


@router.websocket("/ws/autoapply")
async def autoapply_websocket(websocket: WebSocket):
    await websocket.accept()
    shared_db = get_shared_db()

    # --- token handshake: query param first, fall back to first message ---
    token = websocket.query_params.get("token")
    if not token:
        try:
            first_msg = await websocket.receive_json()
            token = first_msg.get("token")
        except Exception:
            token = None

    user_id = await validate_bot_token(token, shared_db) if token else None

    if not user_id:
        await websocket.send_json(TokenInvalidEvent().model_dump())
        await websocket.close()
        return

    user_db = await _resolve_user_db(user_id, shared_db)

    await manager.connect(user_id, websocket)
    await _push_dashboard_update(
        user_id,
        {"type": "bot_status", "online": True, "last_seen": None},
    )

    # tracked so stop_session/application events don't need it re-passed by the bot
    current_session_id: str | None = None

    try:
        while True:
            raw = await websocket.receive_json()

            try:
                event = parse_autoapply_message(raw)
            except (ValueError, Exception):
                # unknown/malformed event -- ignore rather than crash the socket
                continue

            # ---------------- start_session ----------------
            if isinstance(event, StartSessionEvent):
                session = BotSession(user_id=user_id, status="running")
                result = await user_db["bot_sessions"].insert_one(session.to_mongo())
                current_session_id = str(result.inserted_id)
                await _push_dashboard_update(
                    user_id,
                    {"type": "bot_status", "online": True, "last_seen": None},
                )

                await _send_task_or_completion(websocket, user_id, shared_db, user_db, current_session_id)

            # ---------------- stop_session ----------------
            elif isinstance(event, StopSessionEvent):
                await user_db["bot_sessions"].update_one(
                    {"_id": ObjectId(event.session_id)},
                    {"$set": {"status": "stopped", "ended_at": datetime.now(timezone.utc)}},
                )
                # bot is still connected, but no longer actively running a
                # session -- dashboard's bot_status contract only has an
                # online flag, so a stopped session reads as online: False
                await _push_dashboard_update(
                    user_id,
                    {"type": "bot_status", "online": False, "last_seen": None},
                )

                await websocket.send_json(
                    SessionStateEvent(session_id=event.session_id, status="stopped").model_dump()
                )

            # ---------------- jobs_found ----------------
            elif isinstance(event, JobsFoundEvent):
                task_doc = await user_db["job_queue"].find_one({"_id": ObjectId(event.task_id)})
                if not task_doc:
                    continue

                job_request = await user_db["job_requests"].find_one(
                    {"_id": ObjectId(task_doc["job_request_id"])}
                )
                job_type = job_request["job_type"] if job_request else ""
                experience_level = job_request["experience_level"] if job_request else ""

                raw_jobs = [job.model_dump() for job in event.jobs]
                approved_jobs = await filter_matching_jobs(
                    job_type, experience_level, raw_jobs, user_id, shared_db
                )

                # assign a job_id to each approved job and remember its
                # details so application_submitted/skipped/failed (which only
                # carry task_id + job_id) can be turned into a full
                # job_applications row later
                dispatched_jobs = {}
                apply_items = []
                for job in approved_jobs:
                    job_id = str(ObjectId())
                    dispatched_jobs[job_id] = {
                        "role": job.get("title", ""),
                        "company": job.get("company", ""),
                        "link": job.get("link", ""),
                    }
                    apply_items.append(ApplyJobItem(link=job.get("link", ""), job_id=job_id))

                await user_db["job_queue"].update_one(
                    {"_id": ObjectId(event.task_id)},
                    {"$set": {f"dispatched_jobs.{jid}": details for jid, details in dispatched_jobs.items()}},
                )

                await websocket.send_json(
                    ApplyToEvent(task_id=event.task_id, jobs=apply_items).model_dump()
                )

            # ---------------- question_asked ----------------
            elif isinstance(event, QuestionAskedEvent):
                try:
                    answer_text = await generate_answer(
                        user_id, event.question_text, event.field_type, shared_db
                    )
                    await websocket.send_json(
                        AnswerGeneratedEvent(
                            question_id=event.question_id, answer_text=answer_text
                        ).model_dump()
                    )
                except Exception:
                    # AI call failed -- don't leave the bot hanging on this
                    # field; ack so it can move on (skip/leave blank per its
                    # own fallback behavior)
                    await websocket.send_json(
                        {"type": "question_received", "question_id": event.question_id}
                    )

            # ---------------- file_requested ----------------
            # NOT in the original master message contract -- category 7
            # addition so the bot can ask for a resume/cover-letter/etc.
            elif isinstance(event, FileRequestedEvent):
                document = await get_document_for_field(user_id, event.field_label, shared_db)
                if document:
                    await websocket.send_json(
                        FileReadyEvent(
                            request_id=event.request_id,
                            file_url=document.get("url_or_file_ref"),
                            found=True,
                        ).model_dump()
                    )
                else:
                    await websocket.send_json(
                        FileReadyEvent(
                            request_id=event.request_id, file_url=None, found=False
                        ).model_dump()
                    )

            # ---------------- application_submitted / skipped / failed ----------------
            elif isinstance(event, (ApplicationSubmittedEvent, ApplicationSkippedEvent, ApplicationFailedEvent)):
                task_doc = await user_db["job_queue"].find_one({"_id": ObjectId(event.task_id)})
                dispatched = (task_doc or {}).get("dispatched_jobs", {}).get(event.job_id, {})

                status_map = {
                    ApplicationSubmittedEvent: "submitted",
                    ApplicationSkippedEvent: "skipped",
                    ApplicationFailedEvent: "failed",
                }
                app_status = status_map[type(event)]
                reason = getattr(event, "reason", None)

                application_doc = {
                    "user_id": user_id,
                    "task_id": event.task_id,
                    "role": dispatched.get("role", ""),
                    "company": dispatched.get("company", ""),
                    "link": dispatched.get("link", ""),
                    "status": app_status,
                    "reason": reason,
                    "applied_at": datetime.now(timezone.utc),
                }
                await user_db["job_applications"].insert_one(application_doc)
                await _push_dashboard_update(
                    user_id,
                    {
                        "type": "job_progress_update",
                        "job_application": {
                            "id": str(application_doc.get("_id") or ""),
                            "task_id": application_doc["task_id"],
                            "role": application_doc["role"],
                            "company": application_doc["company"],
                            "link": application_doc["link"],
                            "status": application_doc["status"],
                            "reason": application_doc["reason"],
                            "applied_at": application_doc["applied_at"].isoformat(),
                        },
                    },
                )

                await user_db["job_queue"].update_one(
                    {"_id": ObjectId(event.task_id)},
                    {"$set": {"status": "done", "updated_at": datetime.now(timezone.utc)}},
                )

                remaining = await check_daily_limit_remaining(user_id, shared_db, user_db)
                counter_stats = await get_daily_counter_stats(user_id, shared_db, user_db)
                if remaining <= 0:
                    await websocket.send_json(DailyLimitReachedEvent().model_dump())
                    await _push_dashboard_update(
                        user_id,
                        {
                            "type": "daily_counter_update",
                            "applied_today": counter_stats["applied_today"],
                            "limit": counter_stats["limit"],
                        },
                    )
                else:
                    session_id = current_session_id or ""
                    await _send_task_or_completion(websocket, user_id, shared_db, user_db, session_id)
                    await _push_dashboard_update(
                        user_id,
                        {
                            "type": "daily_counter_update",
                            "applied_today": counter_stats["applied_today"],
                            "limit": counter_stats["limit"],
                        },
                    )

            # ---------------- hr_email_found ----------------
            elif isinstance(event, HrEmailFoundEvent):
                # HRContact.source has no direct counterpart in the event --
                # we store the job_link there since it's the only "where this
                # was found" info the bot sends. Revisit if hr_contacts should
                # track job_link as its own field instead.
                hr_contact = HRContact(
                    user_id=user_id,
                    session_id=event.session_id,
                    email=event.email,
                    company=event.company,
                    source=event.job_link,
                )
                hr_contact_doc = hr_contact.to_mongo()
                await user_db["hr_contacts"].insert_one(hr_contact_doc)
                await _push_dashboard_update(
                    user_id,
                    {
                        "type": "hr_contact_added",
                        "hr_contact": {
                            "id": str(hr_contact_doc.get("_id") or ""),
                            "session_id": event.session_id,
                            "email": hr_contact.email,
                            "company": hr_contact.company,
                            "source": hr_contact.source,
                            "found_at": hr_contact.found_at.isoformat(),
                        },
                    },
                )

    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(user_id)
        await _push_dashboard_update(
            user_id,
            {
                "type": "bot_status",
                "online": False,
                "last_seen": datetime.now(timezone.utc).isoformat(),
            },
        )
