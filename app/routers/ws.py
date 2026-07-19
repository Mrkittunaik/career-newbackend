import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.db import get_core_db, get_user_db
from app.core.security import decode_access_token, verify_bot_token
from app.services import ai_brain, agent_brain, session_manager, site_catalog

router = APIRouter(tags=["ws"])

HEARTBEAT_INTERVAL_SECONDS = 25
HEARTBEAT_TIMEOUT_SECONDS = 60  # no pong within this window -> treat as dead


class ConnectionManager:
    """
    Tracks live sockets per user, per kind ('dashboard' or 'bot'), so events can
    be pushed to the right audience. Same tracking pattern for both kinds —
    kept as one manager instead of two separate classes to avoid duplicating
    the connect/disconnect/send bookkeeping.
    """

    def __init__(self):
        self._connections: dict[str, dict[str, set[WebSocket]]] = {"dashboard": {}, "bot": {}}

    async def connect(self, kind: str, user_id: str, websocket: WebSocket):
        await websocket.accept()
        self._connections[kind].setdefault(user_id, set()).add(websocket)

    def disconnect(self, kind: str, user_id: str, websocket: WebSocket):
        conns = self._connections[kind].get(user_id)
        if conns and websocket in conns:
            conns.remove(websocket)
            if not conns:
                self._connections[kind].pop(user_id, None)

    async def send_to_user(self, user_id: str, event_type: str, payload: dict, kind: str = "dashboard"):
        """
        Call this from wherever bot events land server-side to push a live
        update matching one of DashboardSocket's EVENT_TYPES:
        bot_status | job_progress_update | hr_contact_added |
        daily_counter_update | application_reply_received
        Also used, with kind="bot", to send navigation commands down to the
        bot itself (open_site, apply_filters, resume_session, etc.) — same
        delivery mechanism, different audience.
        """
        conns = self._connections[kind].get(user_id, set())
        message = json.dumps({"type": event_type, "payload": payload})
        dead = []
        for ws in conns:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(kind, user_id, ws)

    def is_bot_online(self, user_id: str) -> bool:
        return bool(self._connections["bot"].get(user_id))


manager = ConnectionManager()


async def start_automation_session(
    user_id: str, job_request_id: str, job_type: str, experience_level: str, target_sites: list[str]
) -> bool:
    """
    Entry point called from jobs.py (POST /jobs/request) and chat.py (a
    job_search intent from the AI chat) right after a job_requests document
    is written. This is what turns "user asked for jobs" into the bot
    actually opening a browser tab — without this, a queued job_requests
    row just sat there with nothing consuming it (the old worker-based
    /internal/* path was never actually built).

    Returns False (and does nothing else) if the bot isn't currently
    connected — the job_requests document still exists with status
    "queued", so nothing is lost. The bot_ws() connect handler now drains
    the oldest queued request automatically the next time this user's bot
    comes online (see get_next_queued_job_request), so this is no longer
    a dead end — just deferred until the bot reconnects.
    """
    if not manager.is_bot_online(user_id) or not target_sites:
        return False

    session = await session_manager.start_session(
        user_id, job_request_id, job_type, experience_level, target_sites
    )
    await _open_site_for_session(user_id, session)
    return True


async def _open_site_for_session(user_id: str, session: dict) -> None:
    """Builds the concrete search URL for the site at the session's current
    site_index and sends open_site to the bot. Shared by both the initial
    kickoff and handle_no_more_jobs (after a site's jobs are exhausted)."""
    site_index = session["site_index"]
    target_sites = session["target_sites"]
    session_id = str(session["_id"])

    if site_index >= len(target_sites):
        await session_manager.complete_session(user_id, session_id)
        await manager.send_to_user(user_id, "session_complete", {"session_id": session_id}, kind="bot")
        await _push_status(user_id, session_id, "done")
        await manager.send_to_user(
            user_id,
            "daily_counter_update",
            {"jobs_applied": session["jobs_applied"], "jobs_skipped": session["jobs_skipped"]},
            kind="dashboard",
        )
        return

    site_name = target_sites[site_index]
    built = site_catalog.build_search_url(site_name, session["job_type"], session["experience_level"])

    await session_manager.update_session(
        user_id, session_id, step="opening_site", current_url=built["url"]
    )
    await _push_status(user_id, session_id, "opening_site", site=site_name)
    await manager.send_to_user(
        user_id,
        "open_site",
        {
            "session_id": session_id,
            "site": site_name,
            "url": built["url"],
            "needs_ui_filters": built["needs_ui_filters"],
            "job_type": session["job_type"],
            "experience_level": session["experience_level"],
        },
        kind="bot",
    )


async def _push_status(user_id: str, session_id: str, step: str, site: str | None = None, reason: str | None = None) -> None:
    """Sends the live 'what's happening right now' status card to the
    dashboard/chat UI. Kept as a thin wrapper around session_manager's
    describe_step so every call site pushes the same wording it just
    persisted — one source of truth for the label text."""
    await manager.send_to_user(
        user_id,
        "bot_status_update",
        {"session_id": session_id, "step": step, "message": session_manager.describe_step(step, site, reason)},
        kind="dashboard",
    )


@router.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket):
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="Missing auth token")
        return

    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        if not user_id:
            raise ValueError("no sub claim")
    except Exception:
        await websocket.close(code=4003, reason="Unauthorized")
        return

    await manager.connect("dashboard", user_id, websocket)
    try:
        while True:
            # The dashboard doesn't currently send anything up this socket; we just
            # keep the connection alive and drop/ignore whatever arrives.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect("dashboard", user_id, websocket)


async def _mark_bot_status(user_id: str, online: bool):
    """Keep both collections in sync: bot_sessions (source of truth for A5/A6
    heartbeat logic, per-user content DB) and settings.bot_online (what GET
    /settings reads on initial page load, account-level, hosted core DB).
    Writing only one would leave the other stale — GET /settings would show
    the wrong initial state."""
    user_db = await get_user_db(user_id)
    core_db = get_core_db()
    now = datetime.now(timezone.utc)
    await user_db.bot_sessions.update_one(
        {"user_id": user_id},
        {"$set": {"online": online, "last_seen": now}},
        upsert=True,
    )
    await core_db.settings.update_one(
        {"user_id": user_id},
        {"$set": {"bot_online": online}},
        upsert=True,
    )
    return now


@router.websocket("/ws/bot")
async def bot_ws(websocket: WebSocket):
    # The bot sends the token as `Authorization: Bearer <token>` on the WS
    # handshake (confirmed in careeros-bot/src/shared/connection.js connect()),
    # not a query param — that's the /ws/dashboard convention, not this one.
    auth_header = websocket.headers.get("authorization", "")
    raw_token = auth_header[7:] if auth_header.lower().startswith("bearer ") else None

    if not raw_token:
        await websocket.close(code=4001, reason="Missing bot token")
        return

    # Same lookup verify_bot_token/validate-token uses — hashed tokens mean we
    # scan settings docs with a bot_token_hash and verify each. settings is
    # account-level, always the hosted core DB, and we don't know which
    # user this is yet at this point anyway.
    db = get_core_db()
    user_id = None
    async for doc in db.settings.find({"bot_token_hash": {"$exists": True}}):
        if verify_bot_token(raw_token, doc["bot_token_hash"]):
            user_id = doc["user_id"]
            break

    if user_id is None:
        await websocket.accept()
        # Bot's _handleMessage never closes the socket itself on token_invalid —
        # it just clears state and emits the event. The server must close it,
        # or the socket sits open with a bot that has wiped its own token and
        # won't retry (confirmed in connection.js: no ws.close() in that branch).
        await websocket.send_text(json.dumps({"type": "token_invalid"}))
        await websocket.close(code=4003, reason="Invalid or revoked token")
        return

    await manager.connect("bot", user_id, websocket)
    await _mark_bot_status(user_id, online=True)
    await manager.send_to_user(user_id, "bot_status", {"online": True}, kind="dashboard")

    last_pong = asyncio.get_event_loop().time()

    # Loaded once, lazily, on the first scan_fields — then reused for every
    # subsequent scan on this same connection (including reload-recovery
    # re-scans) so we don't re-hit Mongo for profile/documents every time.
    profile_cache: dict | None = None

    # RECOVERY: if this user had a session mid-flight when they last
    # disconnected (internet drop, bot restart, laptop closed), it's sitting
    # in Mongo as status "in_progress" or "interrupted". Hand it back to the
    # bot immediately on reconnect instead of silently losing that progress
    # — the bot decides how to act on resume_session (e.g. reopen
    # current_url and pick back up at `step`), but the backend is the one
    # telling it that unfinished work exists at all.
    resumable = await session_manager.get_resumable_session(user_id)
    if resumable is not None:
        session_id = str(resumable["_id"])
        await session_manager.update_session(user_id, session_id, status="in_progress")
        await manager.send_to_user(
            user_id,
            "resume_session",
            {
                "session_id": session_id,
                "step": resumable.get("step"),
                "current_url": resumable.get("current_url"),
                "current_job": resumable.get("current_job"),
                "job_type": resumable.get("job_type"),
                "experience_level": resumable.get("experience_level"),
                "target_sites": resumable.get("target_sites"),
                "site_index": resumable.get("site_index", 0),
                "jobs_applied": resumable.get("jobs_applied", 0),
                "jobs_skipped": resumable.get("jobs_skipped", 0),
            },
            kind="bot",
        )
    else:
        # No in-flight session to resume — but the user may have asked for
        # a job search (via chat or the form) while the bot was closed.
        # Previously that request just sat at job_requests.status="queued"
        # forever, since start_automation_session() only fires at the
        # moment the request is created and gives up silently if the bot
        # isn't connected yet. Drain the oldest queued request now instead.
        queued = await session_manager.get_next_queued_job_request(user_id)
        if queued is not None:
            job_request_id = str(queued["_id"])
            session = await session_manager.start_session(
                user_id,
                job_request_id,
                queued.get("job_type", ""),
                queued.get("experience_level", "any"),
                queued.get("target_sites") or [],
            )
            db = await get_user_db(user_id)
            await db.job_requests.update_one(
                {"_id": queued["_id"]}, {"$set": {"status": "processing"}}
            )
            await _open_site_for_session(user_id, session)

    async def heartbeat():
        nonlocal last_pong
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            try:
                await websocket.send_text(json.dumps({"type": "ping"}))
            except Exception:
                return
            if asyncio.get_event_loop().time() - last_pong > HEARTBEAT_TIMEOUT_SECONDS:
                try:
                    await websocket.close(code=4008, reason="Heartbeat timeout")
                except Exception:
                    pass
                return

    hb_task = asyncio.create_task(heartbeat())

    async def handle_open_site_ack(payload: dict):
        """
        Bot confirms the search-results page actually loaded (and, if the
        bot handles login itself, that it's logged in). If this site needs
        UI-based filters (see site_catalog.NEEDS_UI_FILTERS — no clean URL
        param for experience level), the backend now tells the bot exactly
        what filter to apply next; otherwise the URL already encoded the
        filter and the bot goes straight to scanning listings.
        """
        session_id = payload.get("session_id")
        needs_ui_filters = payload.get("needs_ui_filters", False)
        if not session_id:
            return

        if needs_ui_filters:
            await session_manager.update_session(user_id, session_id, step="awaiting_filters")
            await _push_status(user_id, session_id, "awaiting_filters", site=payload.get("site"))
            await manager.send_to_user(
                user_id,
                "apply_filters",
                {"session_id": session_id, "experience_level": payload.get("experience_level")},
                kind="bot",
            )
        else:
            await session_manager.update_session(user_id, session_id, step="scanning")
            await _push_status(user_id, session_id, "scanning", site=payload.get("site"))

    async def handle_filters_applied(payload: dict):
        """Bot confirms it clicked through the on-page filter UI. Either
        way (URL-param site or UI-filter site) the session converges here:
        scanning listings is the next step."""
        session_id = payload.get("session_id")
        if session_id:
            await session_manager.update_session(user_id, session_id, step="scanning")
            await _push_status(user_id, session_id, "scanning", site=payload.get("site"))

    async def handle_no_more_jobs(payload: dict):
        """
        Bot reports it reached the end of this site's listings (no more
        results, or hit the daily/session limit). Backend advances to the
        next target site, or completes the session if that was the last
        one — the bot never has to know how many sites are left or decide
        what "next" means, it just keeps telling the backend what happened
        and waiting for the next instruction.
        """
        session_id = payload.get("session_id")
        if not session_id:
            return
        db = await get_user_db(user_id)
        from bson import ObjectId

        session = await db.automation_sessions.find_one({"_id": ObjectId(session_id)})
        if session is None:
            return
        await session_manager.update_session(
            user_id, session_id, site_index=session["site_index"] + 1, step="opening_site"
        )
        session["site_index"] += 1
        await _open_site_for_session(user_id, session)

    async def handle_scan_fields(payload: dict):
        nonlocal profile_cache
        session_id = payload.get("session_id")
        fields = payload.get("fields", [])

        if profile_cache is None:
            profile_cache = await ai_brain._load_profile_context(user_id)

        if session_id:
            await session_manager.update_session(user_id, session_id, step="filling")
            await _push_status(user_id, session_id, "filling")

        # File-type fields (resume/CV upload) never get an AI text answer —
        # ai_brain explicitly skips them (SKIP_FIELD_TYPES). Instead, resolve
        # which uploaded document to hand the bot up front, so the
        # answers_complete payload can carry either a download path or an
        # explicit warning — never silence. Silently sending nothing left
        # the bot with no way to tell "no file field on this form" apart
        # from "file field present but user never uploaded a resume."
        resume_payload: dict = {}
        if any(f.get("type") == "file" for f in fields):
            doc = ai_brain.select_resume_document(profile_cache)
            if doc:
                resume_payload = {
                    "resume_document_id": doc["id"],
                    "resume_download_path": f"/api/v1/profile/documents/{doc['id']}/download",
                    "resume_filename": doc.get("title"),
                }
            else:
                resume_payload = {
                    "resume_warning": "no_resume_uploaded",
                    "resume_warning_message": (
                        "This application has a resume/file upload field, but no resume "
                        "is uploaded on your CareerOS profile. Upload one under Profile > "
                        "Documents so future applications can attach it automatically."
                    ),
                }

        async def run_stream():
            try:
                async with asyncio.timeout(ai_brain.AI_TIMEOUT_SECONDS + 2):
                    async for index, value in ai_brain.stream_answers(user_id, fields, profile_cache):
                        await websocket.send_text(
                            json.dumps({"type": "answer_chunk", "payload": {"index": index, "value": value}})
                        )
            except (TimeoutError, asyncio.TimeoutError):
                # AI call ran long — send whatever made it through (some chunks
                # may already be sent) and complete anyway so the bot isn't
                # left hanging indefinitely on a stuck provider call.
                pass
            except Exception:
                # Any provider/network failure: complete with zero/partial
                # answers rather than crashing the whole /ws/bot connection.
                pass
            finally:
                await websocket.send_text(
                    json.dumps({
                        "type": "answers_complete",
                        "payload": {"session_id": session_id, **resume_payload},
                    })
                )

        # Fire-and-forget so the main receive loop keeps handling pongs/other
        # messages (e.g. a stop_session) while the AI call is in flight.
        asyncio.create_task(run_stream())

    async def handle_report_result(payload: dict):
        """
        Bot sends this after a fill+submit attempt completes. Writes a real
        job_applications row and pushes job_progress_update to the dashboard
        — the same event/shape internal.py's old /internal/applications
        endpoint already produced, so dashboard.js needs no changes. This
        replaces that REST+shared-secret path for bot traffic, per the
        architecture decision to route all bot-originated events through
        /ws/bot instead.

        Also bumps the owning automation_session's counters (if this report
        belongs to one) so dashboard/session recovery has an accurate
        applied/skipped count without re-deriving it from job_applications
        every time.
        """
        db = await get_user_db(user_id)
        now = datetime.now(timezone.utc)
        status_value = payload.get("status", "pending")
        doc = {
            "user_id": user_id,
            "role": payload.get("role_title") or "Unknown",
            "company": payload.get("company_name") or "Unknown",
            "site": payload.get("source_site_url") or payload.get("job_url") or "",
            "status": status_value,
            "link": payload.get("job_url"),
            "reply_received": False,
            "reply_snippet": None,
            "applied_at": now,
        }
        result = await db.job_applications.insert_one(doc)
        doc_id = str(result.inserted_id)

        session_id = payload.get("session_id")
        if session_id:
            counter_field = "jobs_applied" if status_value == "submitted" else "jobs_skipped"
            await session_manager.increment_counter(user_id, session_id, counter_field)
            await session_manager.update_session(user_id, session_id, step="scanning", current_job=None)

        await manager.send_to_user(
            user_id,
            "job_progress_update",
            {
                "id": doc_id,
                "role": doc["role"],
                "company": doc["company"],
                "site": doc["site"],
                "status": doc["status"],
                "link": doc["link"],
                "reply_received": doc["reply_received"],
                "applied_at": now.isoformat(),
            },
            kind="dashboard",
        )
        await websocket.send_text(json.dumps({"type": "report_ack", "payload": {"id": doc_id}}))

    async def handle_report_problem(payload: dict):
        """
        New, additive message type: the bot sends this when it hits something
        it can't push through on its own mid-task — a broken/dead link, a page
        layout it doesn't recognize, rate limiting, a login wall, etc. Mirrors
        handle_job_decision's shape: the AI resolves it itself by default
        (skip_job / retry_later) and only escalates to the human (ask_user)
        when the situation is genuinely ambiguous or consequential enough that
        guessing wrong would cost the user more than a short pause would.

        On ask_user, the question is pushed to BOTH sockets — "bot" so the bot
        knows to hold this job open pending an answer, and "dashboard" so the
        user actually sees the question surfaced live rather than needing to
        go dig through chat. The next chat message the user sends is a normal
        message; nothing here forces a special reply format from them — the
        bot should treat any answer to the (persisted) pending question as
        the resolution and query for it after a "problem_resolved" ack, since
        this isn't a request/response like job_decision — it can wait an
        arbitrary amount of time for the human.
        """
        nonlocal profile_cache
        problem = payload.get("problem", {})
        session_id = payload.get("session_id")

        if profile_cache is None:
            profile_cache = await ai_brain._load_profile_context(user_id)

        if session_id:
            await session_manager.update_session(user_id, session_id, step="awaiting_decision")

        resolution = await agent_brain.resolve_problem(user_id, problem, profile_cache)

        if resolution["action"] == "ask_user":
            await manager.send_to_user(
                user_id,
                "bot_question",
                {
                    "session_id": session_id,
                    "question": resolution["question_for_user"],
                    "problem": problem,
                },
                kind="dashboard",
            )

        await websocket.send_text(
            json.dumps({"type": "problem_resolution", "payload": {"session_id": session_id, **resolution}})
        )

    async def handle_job_decision(payload: dict):
        """
        New, additive message type: the bot sends this BEFORE scanning a
        job's form fields, describing the job (title, company, description,
        url). The agent decides apply/skip using agent_brain, persists the
        decision (memory across sessions), and replies with job_decision_result
        so the bot only proceeds to scan_fields if told to apply. Older bot
        builds that never send job_decision simply keep using scan_fields
        directly, unaffected by any of this.
        """
        nonlocal profile_cache
        job = payload.get("job", {})
        request_id = payload.get("request_id")
        session_id = payload.get("session_id")

        if profile_cache is None:
            profile_cache = await ai_brain._load_profile_context(user_id)

        if session_id:
            await session_manager.update_session(
                user_id, session_id, step="awaiting_decision", current_job=job
            )
            await _push_status(user_id, session_id, "awaiting_decision")

        existing = await agent_brain.already_decided(user_id, job)
        if existing is not None:
            decision, reason = existing["decision"], existing["reason"]
        else:
            decision_enum, reason = await agent_brain.decide_job(user_id, job, profile_cache)
            decision = decision_enum.value
            await agent_brain.record_decision(user_id, job, decision_enum, reason)

        # Distinct "why" card, separate from the plain status line — the
        # dashboard/chat UI renders this as its own box (decision + reason +
        # which job it was about) rather than folding it into the generic
        # status text, so the user can see the actual reasoning per job
        # without it disappearing the moment the next status update arrives.
        await manager.send_to_user(
            user_id,
            "job_decision_reason",
            {
                "session_id": session_id,
                "job_title": job.get("title"),
                "company": job.get("company"),
                "decision": decision,
                "reason": reason,
            },
            kind="dashboard",
        )

        await websocket.send_text(
            json.dumps(
                {
                    "type": "job_decision_result",
                    "payload": {"request_id": request_id, "decision": decision, "reason": reason},
                }
            )
        )

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            msg_type = msg.get("type")
            if msg_type == "pong":
                last_pong = asyncio.get_event_loop().time()
            elif msg_type == "job_decision":
                await handle_job_decision(msg.get("payload", msg))
            elif msg_type == "report_problem":
                await handle_report_problem(msg.get("payload", msg))
            elif msg_type == "scan_fields":
                await handle_scan_fields(msg.get("payload", msg))
            elif msg_type == "report_result":
                await handle_report_result(msg.get("payload", msg))
            elif msg_type == "site_opened":
                await handle_open_site_ack(msg.get("payload", msg))
            elif msg_type == "filters_applied":
                await handle_filters_applied(msg.get("payload", msg))
            elif msg_type == "no_more_jobs":
                await handle_no_more_jobs(msg.get("payload", msg))
    except WebSocketDisconnect:
        pass
    finally:
        hb_task.cancel()
        manager.disconnect("bot", user_id, websocket)
        now = await _mark_bot_status(user_id, online=False)
        # Don't let a live automation session sit stamped "in_progress"
        # forever with nobody actually running it — flag it interrupted so
        # get_resumable_session() picks it up the next time this user's bot
        # connects, instead of it looking active-but-dead indefinitely.
        await session_manager.mark_interrupted(user_id)
        await manager.send_to_user(
            user_id, "bot_status", {"online": False, "last_seen": now.isoformat()}, kind="dashboard"
        )
