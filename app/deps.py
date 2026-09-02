"""Shared FastAPI dependencies: authentication, quota checks, rate limiting."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from fastapi import Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.errors import AuthError, QuotaExceededError, RateLimitError
from app.models import ApiKey, Monitor, MonthlyUsage, User
from app.plans import PlanSpec, get_plan
from app.security import hash_api_key

BEARER_PREFIX = "bearer "


def _extract_key(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith(BEARER_PREFIX):
        return auth[len(BEARER_PREFIX):].strip()
    return request.headers.get("x-api-key")


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Authenticate via API key (Authorization: Bearer wp_live_... or X-API-Key)."""
    key = _extract_key(request)
    if not key:
        raise AuthError("Missing API key. Send 'Authorization: Bearer <key>' or 'X-API-Key: <key>'.")

    api_key = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(key)))
    if api_key is None or not api_key.is_valid:
        raise AuthError("Invalid or revoked API key.")

    user = db.get(User, api_key.user_id)
    if user is None or not user.is_active:
        raise AuthError("Account is disabled.")

    # Best-effort last-used stamp; at most one write per minute per key.
    now = datetime.now(timezone.utc)
    last = api_key.last_used_at
    if last is None or (now - last).total_seconds() > 60:
        api_key.last_used_at = now
        db.commit()

    request.state.user_id = user.id
    return user


def get_current_plan(user: User = Depends(get_current_user)) -> PlanSpec:
    return get_plan(user.plan)


def enforce_monitor_quota(db: Session, user: User) -> None:
    plan = get_plan(user.plan)
    count = db.scalar(select(func.count(Monitor.id)).where(Monitor.user_id == user.id)) or 0
    if count >= plan.max_monitors:
        raise QuotaExceededError(
            f"Plan '{plan.label}' allows {plan.max_monitors} monitors and you have {count}. "
            f"Upgrade at {settings.public_base_url}/v1/billing/checkout to add more.",
            detail={"plan": plan.key, "max_monitors": plan.max_monitors, "current": count},
        )


def clamp_interval(db: Session, user: User, requested: int) -> int:
    """Silently raising the interval would surprise the user, so we reject instead."""
    plan = get_plan(user.plan)
    if requested < plan.min_interval_seconds:
        raise QuotaExceededError(
            f"Plan '{plan.label}' allows a minimum interval of {plan.min_interval_seconds}s; "
            f"you requested {requested}s.",
            detail={"plan": plan.key, "min_interval_seconds": plan.min_interval_seconds},
        )
    return requested


def current_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def get_usage(db: Session, user: User) -> MonthlyUsage | None:
    return db.scalar(
        select(MonthlyUsage).where(
            MonthlyUsage.user_id == user.id, MonthlyUsage.period == current_period()
        )
    )


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------

class TokenBucket:
    """In-process token bucket, keyed by API key or client IP.

    Deliberately in-process: it needs no Redis, which keeps the single-VPS
    deployment story true. Behind multiple API replicas the effective limit
    multiplies by replica count - swap in a shared store at that point (noted
    in docs/DEPLOY.md).
    """

    def __init__(self, rate_per_minute: int, burst: int):
        self.rate = rate_per_minute / 60.0
        self.burst = burst
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def consume(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.burst), now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens < cost:
                retry_after = (cost - tokens) / self.rate if self.rate > 0 else 60.0
                self._buckets[key] = (tokens, now)
                return False, retry_after
            self._buckets[key] = (tokens - cost, now)

            # Bound memory: drop idle buckets once the map gets large.
            if len(self._buckets) > 10_000:
                cutoff = now - 3600
                self._buckets = {k: v for k, v in self._buckets.items() if v[1] > cutoff}
            return True, 0.0


_limiter = TokenBucket(settings.rate_limit_per_minute, settings.rate_limit_burst)


def rate_limit(request: Request) -> None:
    key = _extract_key(request) or (request.client.host if request.client else "anonymous")
    allowed, retry_after = _limiter.consume(key[:64])
    if not allowed:
        raise RateLimitError(
            f"Rate limit of {settings.rate_limit_per_minute} requests/minute exceeded.",
            detail={"retry_after_seconds": round(retry_after, 1)},
        )
