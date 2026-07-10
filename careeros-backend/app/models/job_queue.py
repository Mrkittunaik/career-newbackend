from datetime import datetime
from typing import Literal

from pydantic import Field

from app.models.base import MongoBaseModel, PyObjectId, utcnow

JobQueueStatus = Literal["pending", "sent", "done"]


class JobQueue(MongoBaseModel):
    job_request_id: PyObjectId
    site: str
    search_query: str
    status: JobQueueStatus = "pending"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
