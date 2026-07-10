from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.models.bot_session import BotSessionStatus
from app.models.job_application import JobApplicationStatus


class SessionResponse(BaseModel):
    id: str
    user_id: str
    status: BotSessionStatus
    started_at: datetime
    ended_at: Optional[datetime] = None


class JobApplicationResponse(BaseModel):
    id: str
    user_id: str
    task_id: str
    role: str
    company: str
    link: str
    status: JobApplicationStatus
    reason: Optional[str] = None
    applied_at: datetime
    reply_received: bool = False
    reply_snippet: Optional[str] = None
    reply_received_at: Optional[datetime] = None


class HRContactResponse(BaseModel):
    id: str
    user_id: str
    session_id: str
    email: str
    company: str
    source: str
    found_at: datetime
