from datetime import datetime

from pydantic import Field

from app.models.base import MongoBaseModel, PyObjectId, utcnow


class HRContact(MongoBaseModel):
    user_id: PyObjectId
    session_id: PyObjectId
    email: str
    company: str
    source: str  # e.g. "job_page", "linkedin", "company_site"
    found_at: datetime = Field(default_factory=utcnow)
