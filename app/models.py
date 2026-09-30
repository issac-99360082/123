"""SQLAlchemy models.

Kept deliberately small: users, their API keys, the monitors they define, the
check history, the change events those produce, and the delivery attempts for
each notification. Billing state lives on the user row and is a mirror of
Stripe - Stripe is always the source of truth.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Plan(str, enum.Enum):
    free = "free"
    pro = "pro"
    business = "business"


class ExtractMode(str, enum.Enum):
    raw = "raw"          # whole response body, normalised
    text = "text"        # HTML stripped to visible text
    css = "css"          # a CSS selector's text content
    regex = "regex"      # first capture group of a regex
    json_path = "json"   # dotted path into a JSON response


class CheckStatus(str, enum.Enum):
    unchanged = "unchanged"
    changed = "changed"
    error = "error"


class DeliveryStatus(str, enum.Enum):
    pending = "pending"
    delivered = "delivered"
    failed = "failed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    plan: Mapped[str] = mapped_column(String(20), default=Plan.free.value, nullable=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), index=True)
    subscription_status: Mapped[str | None] = mapped_column(String(32))
    current_period_end: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Unix timestamp of the newest Stripe event already applied to this account.
    # Stripe does not guarantee delivery order, so without this a stale event can
    # overwrite a newer one.
    last_stripe_event_at: Mapped[int | None] = mapped_column(Integer)

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    monitors: Mapped[list["Monitor"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), default="default")
    # Only a hash is stored. The plaintext key is shown once, at creation.
    prefix: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    user: Mapped[User] = relationship(back_populates="api_keys")

    @property
    def is_valid(self) -> bool:
        return self.revoked_at is None


class Monitor(Base):
    __tablename__ = "monitors"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    method: Mapped[str] = mapped_column(String(8), default="GET", nullable=False)
    request_headers: Mapped[dict | None] = mapped_column(JSON)
    request_body: Mapped[str | None] = mapped_column(Text)

    extract_mode: Mapped[str] = mapped_column(String(16), default=ExtractMode.text.value, nullable=False)
    extract_value: Mapped[str | None] = mapped_column(String(1024))
    ignore_patterns: Mapped[list | None] = mapped_column(JSON)

    interval_seconds: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)

    notify_webhook_url: Mapped[str | None] = mapped_column(String(2048))
    notify_webhook_secret: Mapped[str | None] = mapped_column(String(128))
    notify_email: Mapped[str | None] = mapped_column(String(320))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    paused_reason: Mapped[str | None] = mapped_column(String(255))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    next_check_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_content_hash: Mapped[str | None] = mapped_column(String(64))
    last_content: Mapped[str | None] = mapped_column(Text)
    last_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="monitors")
    checks: Mapped[list["Check"]] = relationship(back_populates="monitor", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_monitors_due", "is_active", "next_check_at"),)


class Check(Base):
    __tablename__ = "checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(1024))

    monitor: Mapped[Monitor] = relationship(back_populates="checks")


class ChangeEvent(Base):
    __tablename__ = "change_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"), index=True)
    check_id: Mapped[int | None] = mapped_column(ForeignKey("checks.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    old_hash: Mapped[str | None] = mapped_column(String(64))
    new_hash: Mapped[str | None] = mapped_column(String(64))
    diff: Mapped[dict | None] = mapped_column(JSON)

    deliveries: Mapped[list["Delivery"]] = relationship(
        back_populates="change_event", cascade="all, delete-orphan"
    )


class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    change_event_id: Mapped[int] = mapped_column(
        ForeignKey("change_events.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    target: Mapped[str] = mapped_column(String(2048), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=DeliveryStatus.pending.value, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(1024))
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    change_event: Mapped[ChangeEvent] = relationship(back_populates="deliveries")


class MonthlyUsage(Base):
    """Per-user check counter, used for quota enforcement and unit economics."""

    __tablename__ = "monthly_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    checks_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    changes_detected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "period", name="uq_usage_user_period"),)


class StripeEvent(Base):
    """Processed Stripe event ids, so webhook redelivery is idempotent."""

    __tablename__ = "stripe_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    processed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
