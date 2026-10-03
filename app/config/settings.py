from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # Application
    app_name: str = "AI Invoice Automation"
    app_version: str = "0.1.0"
    log_level: str = "INFO"

    # Background Processing - polls the inbox automatically instead of
    # requiring process_emails.py to be run manually. Set
    # ENABLE_BACKGROUND_PROCESSING=false in .env to disable.
    enable_background_processing: bool = True
    background_processing_interval_minutes: int = 5

    # Email Processing - Only process new emails since last run
    process_only_new_emails: bool = True
    use_last_processed_timestamp: bool = True
    mark_emails_as_read: bool = True

    # Microsoft Graph API
    microsoft_client_id: Optional[str] = None
    microsoft_client_secret: Optional[str] = None
    microsoft_tenant_id: Optional[str] = None
    outlook_mailbox: Optional[str] = None

    # PostgreSQL - the only storage backend.
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: Optional[str] = None
    postgres_user: Optional[str] = None
    postgres_password: Optional[str] = None
    # Optional - only needed for a managed/cloud Postgres (e.g. Neon) that
    # requires SSL. Leave unset for a local/native PostgreSQL; nothing
    # changes for you if you don't set this in .env.
    postgres_sslmode: Optional[str] = None

    # Gemini (Part 4 - Q&A over invoice data)
    gemini_api_key: Optional[str] = None

    # Admin login (Part 6) - single admin user, simple by design to match
    # the project's scale. Sessions are a signed token, not stored server
    # side.
    admin_username: Optional[str] = None
    admin_password: Optional[str] = None
    session_secret_key: Optional[str] = None

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()