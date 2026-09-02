"""Application configuration.

All settings are read from environment variables (or a local .env file).
See .env.example for the full list and which ones you must supply yourself.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Core ---------------------------------------------------------------
    app_name: str = "Watchpost"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    public_base_url: str = "http://localhost:8000"
    secret_key: str = Field(
        default="dev-insecure-secret-change-me",
        description="Signs session cookies and API-key lookups. MUST be changed in production.",
    )
    log_level: str = "INFO"
    log_json: bool = True

    # --- Database -----------------------------------------------------------
    database_url: str = "sqlite:///./watchpost.db"

    # --- HTTP fetching / crawler etiquette ----------------------------------
    user_agent: str = (
        "WatchpostBot/0.1 (+https://watchpost.example/bot; monitoring on behalf of subscribers)"
    )
    fetch_timeout_seconds: float = 20.0
    fetch_max_bytes: int = 2_000_000
    fetch_max_redirects: int = 5
    respect_robots_txt: bool = True
    min_seconds_between_hits_per_host: int = 20
    allow_private_network_targets: bool = False  # SSRF guard; keep false in production

    # --- Scheduler / worker -------------------------------------------------
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = 20
    worker_concurrency: int = 8
    check_batch_size: int = 50
    consecutive_failures_before_pause: int = 20

    # --- Notifications ------------------------------------------------------
    webhook_timeout_seconds: float = 10.0
    webhook_max_attempts: int = 5
    notification_from_email: str = "alerts@watchpost.example"

    # SMTP is optional: without it, email notifications are queued and logged
    # but not delivered. Webhook delivery works with no third-party account.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True

    # --- Billing (Stripe) ---------------------------------------------------
    # Without these the app runs in "billing disabled" mode: everyone stays on
    # the free plan and the upgrade endpoints return a clear 503.
    stripe_secret_key: str | None = None
    stripe_publishable_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_price_pro: str | None = None
    stripe_price_business: str | None = None

    # --- Rate limiting ------------------------------------------------------
    rate_limit_per_minute: int = 120
    rate_limit_burst: int = 40

    @field_validator("public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def billing_enabled(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_webhook_secret)

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
