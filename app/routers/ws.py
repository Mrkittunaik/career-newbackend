import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.db import get_core_db, get_user_db
from app.core.security import decode_access_token, verify_bot_token
from app.services import ai_brain, agent_brain

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

    async def handle_scan_fields(payload: dict):
        nonlocal profile_cache
        session_id = payload.get("session_id")
        fields = payload.get("fields", [])

        if profile_cache is None:
            profile_cache = await ai_brain._load_profile_context(user_id)

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
                    json.dumps({"type": "answers_complete", "payload": {"session_id": session_id}})
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
        """
        db = await get_user_db(user_id)
        now = datetime.now(timezone.utc)
        doc = {
            "user_id": user_id,
            "role": payload.get("role_title") or "Unknown",
            "company": payload.get("company_name") or "Unknown",
            "site": payload.get("source_site_url") or payload.get("job_url") or "",
            "status": payload.get("status", "pending"),
            "link": payload.get("job_url"),
            "reply_received": False,
            "reply_snippet": None,
            "applied_at": now,
        }
        result = await db.job_applications.insert_one(doc)
        doc_id = str(result.inserted_id)

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

        if profile_cache is None:
            profile_cache = await ai_brain._load_profile_context(user_id)

        existing = await agent_brain.already_decided(user_id, job)
        if existing is not None:
            decision, reason = existing["decision"], existing["reason"]
        else:
            decision_enum, reason = await agent_brain.decide_job(user_id, job, profile_cache)
            decision = decision_enum.value
            await agent_brain.record_decision(user_id, job, decision_enum, reason)

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
            elif msg_type == "scan_fields":
                await handle_scan_fields(msg.get("payload", msg))
            elif msg_type == "report_result":
                await handle_report_result(msg.get("payload", msg))
    except WebSocketDisconnect:
        pass
    finally:
        hb_task.cancel()
        manager.disconnect("bot", user_id, websocket)
        now = await _mark_bot_status(user_id, online=False)
        await manager.send_to_user(
            user_id, "bot_status", {"online": False, "last_seen": now.isoformat()}, kind="dashboard"
        )
