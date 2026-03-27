from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # ── App ───────────────────────────────────────────────────────────────
    app_env:   str = "development"
    app_name:  str = "WhatsApp CRM"

    # ── Security ──────────────────────────────────────────────────────────
    secret_key:        str
    encryption_key:    str
    admin_secret_key:  str = ""
    algorithm:         str = "HS256"
    access_token_expire_minutes:  int = 60
    refresh_token_expire_days:    int = 30

    # ── MongoDB ───────────────────────────────────────────────────────────
    mongodb_url:     str = "mongodb://localhost:27017/"
    mongodb_db_name: str = "whatsapp_business"

    # ── Meta / WhatsApp ───────────────────────────────────────────────────
    meta_app_id:          str = ""
    meta_app_secret:      str = ""
    meta_business_id:     str = ""
    meta_waba_id:         str = ""
    meta_phone_number_id: str = ""
    meta_access_token:    str = ""
    meta_api_version:     str = "v22.0"
    meta_config_id:       str = ""

    # ── Webhooks ──────────────────────────────────────────────────────────
    webhook_base_url:     str = ""
    webhook_verify_token: str = "Gradex@123"

    # ── Google OAuth ──────────────────────────────────────────────────────
    google_client_id:     str = ""
    google_client_secret: str = ""

    # ── URLs ──────────────────────────────────────────────────────────────
    frontend_url: str = "http://localhost:5173"
    backend_url:  str = "http://localhost:8000"

    # ── CORS ──────────────────────────────────────────────────────────────
    cors_origins: str = "http://localhost:5173"

    class Config:
        env_file = ".env"
        extra    = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()