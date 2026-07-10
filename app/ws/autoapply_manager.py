from typing import Dict, Optional

from fastapi import WebSocket


class ConnectionManager:
    """
    Tracks active bot WebSocket connections, one per user_id (a user only ever
    has one Electron bot instance connected at a time -- a second connect for
    the same user_id replaces the first).
    """

    def __init__(self):
        self._connections: Dict[str, WebSocket] = {}

    async def connect(self, user_id: str, websocket: WebSocket) -> None:
        # if a stale connection exists for this user, replace it
        self._connections[user_id] = websocket

    def disconnect(self, user_id: str) -> None:
        self._connections.pop(user_id, None)

    def is_online(self, user_id: str) -> bool:
        return user_id in self._connections

    async def send_to_user(self, user_id: str, message: dict) -> bool:
        """Sends a JSON message to that user's bot connection, if online.
        Returns True if sent, False if the user has no active connection."""
        websocket = self._connections.get(user_id)
        if websocket is None:
            return False
        await websocket.send_json(message)
        return True

    def get_connection(self, user_id: str) -> Optional[WebSocket]:
        return self._connections.get(user_id)


# module-level singleton shared by the /ws/autoapply handler and, later,
# any REST/service code that needs to know bot online-state or push to it
manager = ConnectionManager()
