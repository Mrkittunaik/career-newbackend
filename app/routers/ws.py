import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.db import get_db
from app.core.security import decode_access_token, verify_bot_token

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
    heartbeat logic) and settings.bot_online (what GET /settings reads on
    initial page load, per settings.py). Writing only one would leave the
    other stale — GET /settings would show the wrong initial state."""
    db = get_db()
    now = datetime.now(timezone.utc)
    await db.bot_sessions.update_one(
        {"user_id": user_id},
        {"$set": {"online": online, "last_seen": now}},
        upsert=True,
    )
    await db.settings.update_one(
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
    # scan settings docs with a bot_token_hash and verify each.
    db = get_db()
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

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("type") == "pong":
                last_pong = asyncio.get_event_loop().time()
            # Part B (brain layer) will handle question_detected / page-context
            # messages here once built; A3 only needs connection lifecycle.
    except WebSocketDisconnect:
        pass
    finally:
        hb_task.cancel()
        manager.disconnect("bot", user_id, websocket)
        now = await _mark_bot_status(user_id, online=False)
        await manager.send_to_user(
            user_id, "bot_status", {"online": False, "last_seen": now.isoformat()}, kind="dashboard"
        )
