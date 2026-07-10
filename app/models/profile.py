from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.models.base import MongoBaseModel, PyObjectId, utcnow


class ProfileDocument(BaseModel):
    title: str
    url_or_file_ref: str
    doc_type: str  # e.g. "resume", "cover_letter", "portfolio"


class Profile(MongoBaseModel):
    user_id: PyObjectId
    about_paragraph: Optional[str] = None
    documents: List[ProfileDocument] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
