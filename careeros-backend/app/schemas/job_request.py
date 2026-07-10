from datetime import datetime
from typing import List

from pydantic import BaseModel

from app.models.job_request import JobRequestStatus


class JobRequestCreate(BaseModel):
    job_type: str
    experience_level: str
    target_sites: List[str] = []


class JobRequestResponse(BaseModel):
    id: str
    user_id: str
    job_type: str
    experience_level: str
    target_sites: List[str]
    status: JobRequestStatus
    created_at: datetime
