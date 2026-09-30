"""Pydantic request/response models - the public API contract."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import ExtractMode

MIN_PASSWORD_LENGTH = 10


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth -----------------------------------------------------------------

class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=128)

    @field_validator("password")
    @classmethod
    def _strength(cls, v: str) -> str:
        if v.isdigit() or v.isalpha():
            raise ValueError("Password must mix letters with digits or symbols.")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(default="default", max_length=100)


class ApiKeyOut(ORMModel):
    id: int
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyCreatedOut(ApiKeyOut):
    key: str = Field(description="Shown once. Store it now - it cannot be retrieved again.")


class UserOut(ORMModel):
    id: int
    email: EmailStr
    plan: str
    created_at: datetime
    subscription_status: str | None = None
    current_period_end: datetime | None = None


# --- monitors -------------------------------------------------------------

class MonitorBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(max_length=2048)
    method: str = Field(default="GET", max_length=8)
    request_headers: dict[str, str] | None = None
    request_body: str | None = Field(default=None, max_length=100_000)
    extract_mode: ExtractMode = ExtractMode.text
    extract_value: str | None = Field(default=None, max_length=1024)
    ignore_patterns: list[str] | None = Field(default=None, max_length=20)
    interval_seconds: int = Field(default=3600, ge=60, le=2_592_000)
    notify_webhook_url: str | None = Field(default=None, max_length=2048)
    notify_email: EmailStr | None = None

    @field_validator("method")
    @classmethod
    def _method(cls, v: str) -> str:
        v = v.upper()
        if v not in {"GET", "HEAD", "POST"}:
            raise ValueError("method must be one of GET, HEAD, POST")
        return v

    @field_validator("ignore_patterns")
    @classmethod
    def _patterns_compile(cls, v: list[str] | None) -> list[str] | None:
        for pattern in v or []:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"Invalid ignore pattern {pattern!r}: {exc}") from exc
        return v


class MonitorCreate(MonitorBase):
    pass


class MonitorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    url: str | None = Field(default=None, max_length=2048)
    method: str | None = None
    request_headers: dict[str, str] | None = None
    request_body: str | None = None
    extract_mode: ExtractMode | None = None
    extract_value: str | None = Field(default=None, max_length=1024)
    ignore_patterns: list[str] | None = Field(default=None, max_length=20)
    interval_seconds: int | None = Field(default=None, ge=60, le=2_592_000)
    notify_webhook_url: str | None = Field(default=None, max_length=2048)
    notify_email: EmailStr | None = None
    is_active: bool | None = None


class MonitorOut(ORMModel):
    id: int
    name: str
    url: str
    method: str
    extract_mode: str
    extract_value: str | None = None
    ignore_patterns: list[str] | None = None
    interval_seconds: int
    notify_webhook_url: str | None = None
    notify_email: str | None = None
    is_active: bool
    paused_reason: str | None = None
    consecutive_failures: int
    last_checked_at: datetime | None = None
    next_check_at: datetime | None = None
    last_status_code: int | None = None
    last_content_hash: str | None = None
    last_changed_at: datetime | None = None
    created_at: datetime


class MonitorCreatedOut(MonitorOut):
    webhook_secret: str | None = Field(
        default=None,
        description="Shown once. Use it to verify the X-Watchpost-Signature header.",
    )


class CheckOut(ORMModel):
    id: int
    monitor_id: int
    started_at: datetime
    duration_ms: float | None = None
    status: str
    http_status: int | None = None
    content_hash: str | None = None
    error_type: str | None = None
    error_message: str | None = None


class ChangeEventOut(ORMModel):
    id: int
    monitor_id: int
    created_at: datetime
    old_hash: str | None = None
    new_hash: str | None = None
    diff: dict | None = None


class PreviewRequest(BaseModel):
    """Dry-run an extraction against a live URL before saving a monitor."""

    url: str = Field(max_length=2048)
    method: str = "GET"
    extract_mode: ExtractMode = ExtractMode.text
    extract_value: str | None = Field(default=None, max_length=1024)
    ignore_patterns: list[str] | None = Field(default=None, max_length=20)
    request_headers: dict[str, str] | None = None


class PreviewResponse(BaseModel):
    url: str
    http_status: int
    elapsed_ms: float
    content_hash: str
    extracted: str
    extracted_chars: int
    truncated: bool


class UsageOut(BaseModel):
    period: str
    plan: str
    checks_used: int
    checks_included: int
    changes_detected: int
    monitors_used: int
    monitors_allowed: int
    min_interval_seconds: int


class PlanOut(BaseModel):
    key: str
    label: str
    price_usd_month: int
    max_monitors: int
    min_interval_seconds: int
    max_checks_per_month: int
    history_days: int
    current: bool = False


class CheckoutSessionOut(BaseModel):
    checkout_url: str


class PortalSessionOut(BaseModel):
    portal_url: str


class AccountExportOut(BaseModel):
    """Everything the service holds about one account (privacy.md export right)."""

    account: dict
    monitors: list[dict]
    checks: list[dict]
    change_events: list[dict]


class MessageOut(BaseModel):
    message: str
