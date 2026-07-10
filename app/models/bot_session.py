from datetime import datetime
from typing import Literal, Optional

from pydantic import Field

from app.models.base import MongoBaseModel, PyObjectId, utcnow

BotSessionStatus = Literal["running", "stopped", "completed"]


class BotSession(MongoBaseModel):
    user_id: PyObjectId
    status: BotSessionStatus = "running"
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: Optional[datetime] = None
