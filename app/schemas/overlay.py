from typing import Optional

from pydantic import BaseModel


class ValidateTokenRequest(BaseModel):
    token: str


class ValidateTokenResponse(BaseModel):
    valid: bool
    user_id: Optional[str] = None


class RegenerateTokenResponse(BaseModel):
    token: str  # raw token, shown once
    created_at: str
