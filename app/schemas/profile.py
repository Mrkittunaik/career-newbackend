from typing import List, Optional

from pydantic import BaseModel


class DocumentUpload(BaseModel):
    title: str
    url_or_file_ref: str
    doc_type: str


class ProfileUpdate(BaseModel):
    about_paragraph: Optional[str] = None
    documents: Optional[List[DocumentUpload]] = None


class ProfileResponse(BaseModel):
    user_id: str
    about_paragraph: Optional[str] = None
    documents: List[DocumentUpload] = []
