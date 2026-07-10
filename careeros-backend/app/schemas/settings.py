from typing import Optional

from pydantic import BaseModel

from app.models.user_settings import AIProvider, StorageMode


class StorageSettingsUpdate(BaseModel):
    storage_mode: StorageMode
    mongo_url: Optional[str] = None  # raw; backend encrypts before storing. Required if mode == "own"


class AIProviderSettingsUpdate(BaseModel):
    ai_provider: AIProvider
    ai_key: Optional[str] = None  # raw; backend encrypts before storing. Omit to use default Groq key
