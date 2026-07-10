from datetime import datetime
from typing import List, Literal

from pydantic import Field

from app.models.base import MongoBaseModel, PyObjectId, utcnow

JobRequestStatus = Literal["pending", "active", "paused", "completed", "cancelled"]


class JobRequest(MongoBaseModel):
    user_id: PyObjectId
    job_type: str
    experience_level: str
    target_sites: List[str] = Field(default_factory=list)
    status: JobRequestStatus = "pending"
    created_at: datetime = Field(default_factory=utcnow)
