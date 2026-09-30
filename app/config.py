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
    # Wall-clock ceiling for ONE check, across every redirect hop and throttle
    # wait. Without it, a redirect chain multiplies fetch_timeout by the hop
    # count and a hostile site can hold a worker thread for minutes.
    fetch_total_deadline_seconds: float = 45.0
    respect_robots_txt: bool = True
    min_seconds_between_hits_per_host: int = 20
    allow_private_network_targets: bool = False  # SSRF guard; keep false in production

    # --- Scheduler / worker -------------------------------------------------
    scheduler_enabled: bool = True
    # Sustained capacity is check_batch_size / scheduler_tick_seconds and
    # nothing else, and it must exceed what the largest plan promises - see
    # capacity_report(), which warns at startup when it does not.
    scheduler_tick_seconds: int = 10
    worker_concurrency: int = 16
    check_batch_size: int = 200
    consecutive_failures_before_pause: int = 20
    # A claim is a LEASE, not a reschedule. If the worker dies mid-check the
    # lease expires and the work is retried, instead of being silently skipped
    # until the monitor's next natural slot.
    claim_lease_seconds: int = 180
    # Fairness: no single account may take more than this share of one batch,
    # so one user with many due monitors cannot delay everybody else.
    max_batch_share_per_user: float = 0.25
    # How many times an empty extraction must repeat before it is accepted as a
    # real change. Guards against transient blank pages during a deploy.
    empty_content_confirmations: int = 2

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

    # Auth endpoints need far tighter limits than the general API: signup is
    # the door to free resources, and login is a password oracle.
    signup_limit_per_hour_per_ip: int = 5
    login_max_failures: int = 8
    login_lockout_minutes: int = 15
    # A single account may absorb this many failures across ALL addresses before
    # it locks. Set well above login_max_failures: a per-account lock that trips
    # too easily lets anyone who knows an email deny that user access.
    login_max_failures_global: int = 50

    # X-Forwarded-For is only meaningful behind a proxy that OVERWRITES it.
    # Exposed directly, the client sets it freely and picks its own rate-limit
    # bucket, which defeats every per-IP control. Off unless you run a proxy.
    trust_proxy_headers: bool = False

    # --- Retention ----------------------------------------------------------
    retention_enabled: bool = True
    retention_sweep_minutes: int = 60
    retention_batch_size: int = 5_000

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

    @property
    def sustained_checks_per_second(self) -> float:
        """The scheduler claims at most check_batch_size monitors per tick, so
        this - not CPU - is the ceiling on how many checks the deployment can
        actually sustain."""
        return self.check_batch_size / max(1, self.scheduler_tick_seconds)

    def capacity_report(self) -> dict:
        """Configured capacity versus what the plans promise, for startup logging."""
        from app.plans import PLANS

        capacity = self.sustained_checks_per_second
        worst_plan = max(
            PLANS.values(), key=lambda p: p.max_monitors / p.min_interval_seconds
        )
        required = worst_plan.max_monitors / worst_plan.min_interval_seconds
        return {
            "sustained_checks_per_second": round(capacity, 2),
            "checks_per_month": int(capacity * 86_400 * 30),
            "monitors_at_1min": int(capacity * 60),
            "monitors_at_5min": int(capacity * 300),
            "largest_plan": worst_plan.key,
            "largest_plan_requires_checks_per_second": round(required, 2),
            "can_serve_one_largest_plan_customer": capacity >= required,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
