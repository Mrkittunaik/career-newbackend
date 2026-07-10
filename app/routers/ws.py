import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.security import decode_access_token

router = APIRouter(tags=["ws"])


class ConnectionManager:
    """Tracks live dashboard sockets per user so bot-side events can be pushed to them."""

    def __init__(self):
        self._connections: dict[str, set[WebSocket]] = {}

    async def connect(self, user_id: str, websocket: WebSocket):
        await websocket.accept()
        self._connections.setdefault(user_id, set()).add(websocket)

    def disconnect(self, user_id: str, websocket: WebSocket):
        conns = self._connections.get(user_id)
        if conns and websocket in conns:
            conns.remove(websocket)
            if not conns:
                self._connections.pop(user_id, None)

    async def send_to_user(self, user_id: str, event_type: str, payload: dict):
        """
        Call this from wherever bot events land server-side (e.g. a worker
        process writing job_applications, or an internal webhook) to push a
        live update matching one of DashboardSocket's EVENT_TYPES:
        bot_status | job_progress_update | hr_contact_added |
        daily_counter_update | application_reply_received
        """
        conns = self._connections.get(user_id, set())
        message = json.dumps({"type": event_type, "payload": payload})
        dead = []
        for ws in conns:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(user_id, ws)


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

    await manager.connect(user_id, websocket)
    try:
        while True:
            # The dashboard doesn't currently send anything up this socket; we just
            # keep the connection alive and drop/ignore whatever arrives.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)
