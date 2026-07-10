from datetime import datetime
from typing import Optional

from pydantic import Field

from app.models.base import MongoBaseModel, PyObjectId, utcnow


class BotToken(MongoBaseModel):
    user_id: PyObjectId
    token_hash: str
    created_at: datetime = Field(default_factory=utcnow)
    revoked_at: Optional[datetime] = None
