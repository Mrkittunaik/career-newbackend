from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.models.base import MongoBaseModel, PyObjectId, utcnow


class OAuthTokens(BaseModel):
    # stored encrypted at rest (encrypted by the service layer before insert,
    # decrypted after read) -- fields mirror Google's token response shape
    access_token_encrypted: str
    refresh_token_encrypted: Optional[str] = None
    expiry: Optional[datetime] = None
    scope: Optional[str] = None
    token_type: Optional[str] = "Bearer"


class GmailConnection(MongoBaseModel):
    user_id: PyObjectId
    oauth_tokens: OAuthTokens
    connected_at: datetime = Field(default_factory=utcnow)
    # tracks the inbox scan watermark so scan_inbox_for_replies only refetches
    # what it hasn't already seen; None until the first scan completes
    last_scanned_at: Optional[datetime] = None
