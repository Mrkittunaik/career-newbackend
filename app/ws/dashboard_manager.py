from typing import Dict, Optional

from fastapi import WebSocket


class DashboardConnectionManager:
    """
    Tracks active website WebSocket connections, one per user_id (a user only
    ever has one dashboard tab connected at a time -- a second connect for the
    same user_id replaces the first).

    Same pattern as app.ws.autoapply_manager.ConnectionManager, but this
    channel is push-only from the backend: the website never sends events we
    need to act on, so there's no bot-style request/response handling here --
    just register/unregister and push_to_user.
    """

    def __init__(self):
        self._connections: Dict[str, WebSocket] = {}

    async def connect(self, user_id: str, websocket: WebSocket) -> None:
        # if a stale connection exists for this user (e.g. duplicate tab,
        # or a reconnect that raced the old socket's disconnect), replace it
        self._connections[user_id] = websocket

    def disconnect(self, user_id: str, websocket: Optional[WebSocket] = None) -> None:
        """Removes the connection for user_id. If websocket is provided, only
        removes it if it's still the currently-registered connection for that
        user -- avoids a slow-closing old socket's disconnect clobbering a
        newer one that already replaced it."""
        if websocket is not None and self._connections.get(user_id) is not websocket:
            return
        self._connections.pop(user_id, None)

    def is_online(self, user_id: str) -> bool:
        return user_id in self._connections

    async def push_to_user(self, user_id: str, event: dict) -> bool:
        """Sends a JSON event to that user's dashboard connection, if online.
        Returns True if sent, False if the user has no active connection
        (e.g. they don't have the website open) -- callers should treat False
        as a normal, non-error outcome since this channel is best-effort."""
        websocket = self._connections.get(user_id)
        if websocket is None:
            return False
        await websocket.send_json(event)
        return True

    def get_connection(self, user_id: str) -> Optional[WebSocket]:
        return self._connections.get(user_id)


# module-level singleton shared by the /ws/dashboard handler and any
# REST/service/other-websocket code that needs to push live updates to a
# user's website session (e.g. app.ws.autoapply_handler)
manager = DashboardConnectionManager()
