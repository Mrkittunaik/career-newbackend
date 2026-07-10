from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Mongo
    MONGO_URL: str
    DB_NAME: str

    # Auth
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days default

    # Encryption (Fernet key for mongo_url_encrypted / ai_key_encrypted)
    ENCRYPTION_KEY: str

    # AI
    GROQ_API_KEY: str

    # Gmail OAuth
    GOOGLE_CLIENT_ID: str
    GOOGLE_CLIENT_SECRET: str
    GOOGLE_REDIRECT_URI: str  # must exactly match the URI registered in Google Cloud Console,
    # e.g. "https://api.careeros.app/settings/gmail/callback"

    # Website (used for post-OAuth and other backend-initiated redirects)
    WEBSITE_SETTINGS_URL: str = "https://careeros.app/settings"

    # CORS
    WEBSITE_ORIGIN: str = "https://careeros.app"

    # Plans
    DEFAULT_DAILY_JOB_LIMIT: int = 20

    # File storage (see file_service.py for the local-disk-vs-S3 note)
    UPLOAD_DIR: str = "storage/resumes"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    # cached so .env is only parsed once per process
    return Settings()


settings = get_settings()
