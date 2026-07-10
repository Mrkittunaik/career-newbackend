from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db_name: str = "careeros"
    jwt_secret: str = "insecure-dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7  # 7 days
    cors_origins: str = "http://localhost:5500,http://127.0.0.1:5500"

    # Google Sign-In
    google_client_id: str = ""

    # Gmail OAuth
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_redirect_uri: str = "http://localhost:8000/api/v1/settings/gmail/callback"

    # Razorpay
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # AI providers
    default_ai_provider: str = "ours"
    openai_api_key: str = ""
    groq_api_key: str = ""
    anthropic_api_key: str = ""

    # Shared secret the worker process uses to call internal endpoints
    # (push websocket events, write scan results). Not exposed to the frontend.
    internal_api_secret: str = "insecure-dev-internal-secret-change-me"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def gmail_configured(self) -> bool:
        return bool(self.gmail_client_id and self.gmail_client_secret)

    @property
    def razorpay_configured(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def google_signin_configured(self) -> bool:
        return bool(self.google_client_id)


settings = Settings()
