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


class SlidingWindow:
    """Counts events per key inside a rolling window. Used for signup abuse and
    failed-login lockout, where a burst allowance would defeat the purpose."""

    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> list[float]:
        hits = [t for t in self._events.get(key, []) if now - t < self.window]
        self._events[key] = hits
        return hits

    def check(self, key: str) -> tuple[bool, float]:
        """Report whether `key` is under the limit, without recording anything."""
        now = time.monotonic()
        with self._lock:
            hits = self._prune(key, now)
            if len(hits) >= self.limit:
                return False, self.window - (now - hits[0])
            return True, 0.0

    def record(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(key, now).append(now)
            if len(self._events) > 10_000:
                self._events = {k: v for k, v in self._events.items() if v}

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)

    def consume(self, key: str) -> tuple[bool, float]:
        allowed, retry_after = self.check(key)
        if allowed:
            self.record(key)
        return allowed, retry_after


_signup_limiter = SlidingWindow(settings.signup_limit_per_hour_per_ip, 3600)
# Two tiers, so brute-force protection does not become a denial-of-service tool.
# Tight per (account, source) - that is the shape of an actual guessing attack.
_login_failures = SlidingWindow(settings.login_max_failures, settings.login_lockout_minutes * 60)
# Loose per account across all sources - a genuine distributed attack still
# trips it, but one person spamming a known email cannot cheaply lock its owner out.
_login_failures_global = SlidingWindow(
    settings.login_max_failures_global, settings.login_lockout_minutes * 60
)


def client_ip(request: Request) -> str:
    """Client address, trusting X-Forwarded-For only when configured to.

    Honouring the header unconditionally is a self-inflicted bypass: the client
    chooses the value, so it chooses its own rate-limit bucket. Measured with
    the header trusted, one attacker created 20/20 accounts against a limit of
    5. It is now used only when TRUST_PROXY_HEADERS is on, which is correct
    exactly when a reverse proxy overwrites the header.
    """
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "anonymous")[:64]


def signup_rate_limit(request: Request) -> None:
    """Throttle account creation per IP.

    Without this, one host can mint unlimited free accounts and with them
    unlimited free monitors - the service pays for every one of those checks.
    """
    allowed, retry_after = _signup_limiter.consume(f"signup:{client_ip(request)}")
    if not allowed:
        raise RateLimitError(
            f"Only {settings.signup_limit_per_hour_per_ip} accounts may be created per hour "
            "from one address.",
            detail={"retry_after_seconds": round(retry_after, 1)},
        )


def _login_limiters(email: str, source: str) -> tuple[tuple[SlidingWindow, str], ...]:
    """The two tiers guarding sign-in, tightest first.

    Tier 1 is per (account, source) - the shape of a real guessing attack.
    Tier 2 is per account across all sources, set much looser: a lock that trips
    on the account alone lets anyone who knows an email deny its owner access.
    """
    email = email.lower()
    return (
        (_login_failures, f"login:{email}:{source}"),
        (_login_failures_global, f"login:{email}"),
    )


def check_login_lockout(email: str, source: str) -> None:
    for limiter, key in _login_limiters(email, source):
        allowed, retry_after = limiter.check(key)
        if not allowed:
            raise RateLimitError(
                "Too many failed sign-in attempts. Try again later.",
                detail={"retry_after_seconds": round(retry_after, 1)},
            )


def record_login_failure(email: str, source: str) -> None:
    for limiter, key in _login_limiters(email, source):
        limiter.record(key)


def clear_login_failures(email: str, source: str) -> None:
    for limiter, key in _login_limiters(email, source):
        limiter.reset(key)


def rate_limit(request: Request) -> None:
    # Authenticated callers key on their API key; anonymous ones on client_ip(),
    # which respects TRUST_PROXY_HEADERS. Using request.client.host directly
    # would put all anonymous traffic behind a proxy into ONE bucket, letting a
    # single abuser stop every new signup.
    key = _extract_key(request) or client_ip(request)
    allowed, retry_after = _limiter.consume(key[:64])
    if not allowed:
        raise RateLimitError(
            f"Rate limit of {settings.rate_limit_per_minute} requests/minute exceeded.",
            detail={"retry_after_seconds": round(retry_after, 1)},
        )
