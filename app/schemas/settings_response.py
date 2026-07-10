from typing import Optional

from pydantic import BaseModel

from app.models.user_settings import AIProvider, StorageMode


class StorageTestResult(BaseModel):
    """Generic success envelope for /settings/storage and /settings/ai-provider."""
    success: bool
    message: str


class StorageTestFailedResponse(BaseModel):
    """Returned (still HTTP 200 -- this is an expected, well-formed outcome,
    not a server error) when the mongo_url connection test fails, so nothing
    was saved. Explicit storage_mode echo lets the frontend confirm what mode
    was attempted."""
    success: bool = False
    storage_mode: StorageMode
    message: str


class SettingsResponse(BaseModel):
    storage_mode: StorageMode
    mongo_url_masked: Optional[str] = None  # e.g. "••••ab12"; None if not set
    ai_provider: AIProvider
    ai_key_masked: Optional[str] = None
    gmail_connected: bool


class GmailConnectResponse(BaseModel):
    auth_url: str
