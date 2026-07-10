from datetime import datetime
from typing import Literal, Optional

from pydantic import Field

from app.models.base import MongoBaseModel, PyObjectId, utcnow

JobApplicationStatus = Literal["submitted", "skipped", "failed"]


class JobApplication(MongoBaseModel):
    user_id: PyObjectId
    task_id: PyObjectId
    role: str
    company: str
    link: str
    status: JobApplicationStatus
    reason: Optional[str] = None  # populated for skipped/failed
    applied_at: datetime = Field(default_factory=utcnow)

    # populated by gmail_service.scan_inbox_for_replies once a matching
    # inbound email is found for this application
    reply_received: bool = False
    reply_snippet: Optional[str] = None
    reply_received_at: Optional[datetime] = None
