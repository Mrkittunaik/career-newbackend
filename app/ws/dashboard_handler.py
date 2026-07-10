from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.security import decode_access_token
from app.ws.dashboard_manager import manager

router = APIRouter()


async def _authenticate(websocket: WebSocket) -> str | None:
    """Same token-handshake shape as /ws/autoapply: query param first, then
    fall back to the Authorization header (website clients that can set
    headers on the WS upgrade request may prefer that over a query param).
    Returns the user_id (JWT `sub` claim) or None if auth fails."""
    token = websocket.query_params.get("token")

    if not token:
        auth_header = websocket.headers.get("authorization") or websocket.headers.get("Authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1]

    if not token:
        return None

    try:
        payload = decode_access_token(token)
    except Exception:
        # decode_access_token raises HTTPException on expired/invalid tokens --
        # any failure here just means "auth failed", so close the socket
        return None

    return payload.get("sub")


@router.websocket("/ws/dashboard")
async def dashboard_websocket(websocket: WebSocket):
    await websocket.accept()

    user_id = await _authenticate(websocket)
    if not user_id:
        await websocket.close(code=4401)
        return

    await manager.connect(user_id, websocket)

    try:
        while True:
            # push-only channel: the backend never expects meaningful messages
            # from the website side. We still run the receive loop so we can
            # detect disconnects and respond to ping/pong keep-alives; any
            # other payload is just ignored.
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            # starlette/fastapi handle low-level ws ping/pong frames
            # automatically -- app-level "ping" text/json messages (some
            # browser WS clients send these instead of protocol-level pings)
            # get a matching "pong" so the client's keep-alive logic is happy
            text = message.get("text")
            if text is not None and text.strip().lower() in ("ping", '{"type":"ping"}', '{"type": "ping"}'):
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(user_id, websocket)
