from datetime import datetime
from typing import Literal, Optional

from pydantic import Field

from app.models.base import MongoBaseModel, PyObjectId, utcnow

StorageMode = Literal["shared", "own"]
AIProvider = Literal["groq", "openai", "claude"]


class UserSettings(MongoBaseModel):
    user_id: PyObjectId
    storage_mode: StorageMode = "shared"
    mongo_url_encrypted: Optional[str] = None  # only set when storage_mode == "own"
    ai_provider: AIProvider = "groq"
    ai_key_encrypted: Optional[str] = None  # only set when using a bring-your-own-key provider
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
