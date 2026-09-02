"""Monitor CRUD, manual runs, history, and extraction preview."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.engine import run_check
from app.core.extractor import ExtractionError, content_hash, extract
from app.core.fetcher import FetchError, fetch, validate_target_url
from app.db import get_db
from app.deps import (
    clamp_interval,
    enforce_monitor_quota,
    get_current_user,
    get_usage,
    rate_limit,
)
from app.errors import AppError, NotFoundError, ValidationFailedError
from app.logging_conf import get_logger
from app.models import ChangeEvent, Check, Monitor, User
from app.plans import get_plan
from app.schemas import (
    ChangeEventOut,
    CheckOut,
    MessageOut,
    MonitorCreate,
    MonitorCreatedOut,
    MonitorOut,
    MonitorUpdate,
    PreviewRequest,
    PreviewResponse,
    UsageOut,
)
from app.security import generate_webhook_secret

router = APIRouter(prefix="/v1", tags=["monitors"], dependencies=[Depends(rate_limit)])
log = get_logger(__name__)


def _get_owned_monitor(db: Session, user: User, monitor_id: int) -> Monitor:
    monitor = db.scalar(
        select(Monitor).where(Monitor.id == monitor_id, Monitor.user_id == user.id)
    )
    if monitor is None:
        raise NotFoundError(f"Monitor {monitor_id} not found.")
    return monitor


def _validate_monitor_urls(url: str | None, webhook_url: str | None) -> None:
    """Reject unsafe URLs at write time, so the user finds out immediately.

    A DNS failure is deliberately *not* fatal here: a transient resolver outage
    (or a host that is briefly down) must not block someone from saving a
    monitor. The same guard runs again at fetch time, where resolution is
    re-checked before any connection is made, so this does not weaken the
    SSRF protection.
    """
    for value, field in ((url, "url"), (webhook_url, "notify_webhook_url")):
        if not value:
            continue
        try:
            validate_target_url(value)
        except FetchError as exc:
            if exc.kind == "dns_error":
                log.info("url_dns_unresolved_at_write", field=field, url=value[:200])
                continue
            raise ValidationFailedError(f"Invalid {field}: {exc.message}") from exc


@router.post("/monitors", response_model=MonitorCreatedOut, status_code=status.HTTP_201_CREATED)
def create_monitor(
    payload: MonitorCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MonitorCreatedOut:
    enforce_monitor_quota(db, user)
    interval = clamp_interval(db, user, payload.interval_seconds)
    _validate_monitor_urls(payload.url, payload.notify_webhook_url)

    if payload.extract_mode.value in {"css", "regex", "json"} and not payload.extract_value:
        raise ValidationFailedError(
            f"extract_mode '{payload.extract_mode.value}' requires extract_value."
        )
    if not payload.notify_webhook_url and not payload.notify_email:
        raise ValidationFailedError(
            "Set notify_webhook_url and/or notify_email, otherwise changes go nowhere."
        )

    secret = generate_webhook_secret() if payload.notify_webhook_url else None
    monitor = Monitor(
        user_id=user.id,
        name=payload.name,
        url=payload.url,
        method=payload.method,
        request_headers=payload.request_headers,
        request_body=payload.request_body,
        extract_mode=payload.extract_mode.value,
        extract_value=payload.extract_value,
        ignore_patterns=payload.ignore_patterns,
        interval_seconds=interval,
        notify_webhook_url=payload.notify_webhook_url,
        notify_webhook_secret=secret,
        notify_email=payload.notify_email,
        next_check_at=datetime.now(timezone.utc),
    )
    db.add(monitor)
    db.commit()
    db.refresh(monitor)
    log.info("monitor_created", monitor_id=monitor.id, user_id=user.id)

    return MonitorCreatedOut(
        **MonitorOut.model_validate(monitor).model_dump(), webhook_secret=secret
    )


@router.get("/monitors", response_model=list[MonitorOut])
def list_monitors(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    active: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[MonitorOut]:
    stmt = select(Monitor).where(Monitor.user_id == user.id)
    if active is not None:
        stmt = stmt.where(Monitor.is_active.is_(active))
    monitors = db.scalars(stmt.order_by(Monitor.created_at.desc()).limit(limit).offset(offset)).all()
    return [MonitorOut.model_validate(m) for m in monitors]


@router.get("/monitors/{monitor_id}", response_model=MonitorOut)
def get_monitor(
    monitor_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MonitorOut:
    return MonitorOut.model_validate(_get_owned_monitor(db, user, monitor_id))


@router.patch("/monitors/{monitor_id}", response_model=MonitorOut)
def update_monitor(
    monitor_id: int,
    payload: MonitorUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MonitorOut:
    monitor = _get_owned_monitor(db, user, monitor_id)
    data = payload.model_dump(exclude_unset=True)

    if "interval_seconds" in data and data["interval_seconds"] is not None:
        data["interval_seconds"] = clamp_interval(db, user, data["interval_seconds"])
    _validate_monitor_urls(data.get("url"), data.get("notify_webhook_url"))

    if "extract_mode" in data and data["extract_mode"] is not None:
        data["extract_mode"] = data["extract_mode"].value
    if "notify_email" in data and data["notify_email"] is not None:
        data["notify_email"] = str(data["notify_email"])

    # Changing what we extract invalidates the stored baseline: the next check
    # must record a fresh first observation rather than report a false change.
    if any(
        key in data and data[key] != getattr(monitor, key)
        for key in ("url", "extract_mode", "extract_value", "ignore_patterns", "method")
    ):
        monitor.last_content_hash = None
        monitor.last_content = None

    if data.get("notify_webhook_url") and not monitor.notify_webhook_secret:
        monitor.notify_webhook_secret = generate_webhook_secret()

    if data.get("is_active") is True and not monitor.is_active:
        monitor.consecutive_failures = 0
        monitor.paused_reason = None
        monitor.next_check_at = datetime.now(timezone.utc)

    for key, value in data.items():
        setattr(monitor, key, value)

    db.commit()
    db.refresh(monitor)
    return MonitorOut.model_validate(monitor)


@router.delete("/monitors/{monitor_id}", response_model=MessageOut)
def delete_monitor(
    monitor_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageOut:
    monitor = _get_owned_monitor(db, user, monitor_id)
    db.delete(monitor)
    db.commit()
    return MessageOut(message=f"Monitor {monitor_id} deleted.")


@router.post("/monitors/{monitor_id}/run", response_model=CheckOut)
def run_monitor_now(
    monitor_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CheckOut:
    """Run a check immediately, outside the schedule. Useful for testing a new monitor."""
    monitor = _get_owned_monitor(db, user, monitor_id)
    check = run_check(db, monitor)
    db.commit()
    db.refresh(check)
    return CheckOut.model_validate(check)


@router.get("/monitors/{monitor_id}/checks", response_model=list[CheckOut])
def list_checks(
    monitor_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[CheckOut]:
    _get_owned_monitor(db, user, monitor_id)
    plan = get_plan(user.plan)
    cutoff = datetime.now(timezone.utc) - timedelta(days=plan.history_days)
    checks = db.scalars(
        select(Check)
        .where(Check.monitor_id == monitor_id, Check.started_at >= cutoff)
        .order_by(Check.started_at.desc())
        .limit(limit)
    ).all()
    return [CheckOut.model_validate(c) for c in checks]


@router.get("/monitors/{monitor_id}/changes", response_model=list[ChangeEventOut])
def list_changes(
    monitor_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ChangeEventOut]:
    _get_owned_monitor(db, user, monitor_id)
    plan = get_plan(user.plan)
    cutoff = datetime.now(timezone.utc) - timedelta(days=plan.history_days)
    events = db.scalars(
        select(ChangeEvent)
        .where(ChangeEvent.monitor_id == monitor_id, ChangeEvent.created_at >= cutoff)
        .order_by(ChangeEvent.created_at.desc())
        .limit(limit)
    ).all()
    return [ChangeEventOut.model_validate(e) for e in events]


@router.post("/preview", response_model=PreviewResponse)
def preview(
    payload: PreviewRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PreviewResponse:
    """Fetch a URL once and show exactly what would be compared.

    This is the onboarding step that makes selectors usable: the user sees the
    extracted text before committing to a monitor.
    """
    try:
        result = fetch(
            payload.url,
            method=payload.method,
            headers=payload.request_headers,
        )
    except FetchError as exc:
        raise AppError(f"Could not fetch URL: {exc.message}", detail={"kind": exc.kind}) from exc

    try:
        extracted = extract(
            result.body,
            mode=payload.extract_mode.value,
            selector=payload.extract_value,
            ignore_patterns=payload.ignore_patterns,
        )
    except ExtractionError as exc:
        raise AppError(f"Extraction failed: {exc.message}", detail={"kind": exc.kind}) from exc

    return PreviewResponse(
        url=result.url,
        http_status=result.status_code,
        elapsed_ms=round(result.elapsed_ms, 2),
        content_hash=content_hash(extracted),
        extracted=extracted[:10_000],
        extracted_chars=len(extracted),
        truncated=result.truncated or len(extracted) > 10_000,
    )


@router.get("/usage", response_model=UsageOut)
def usage(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UsageOut:
    plan = get_plan(user.plan)
    record = get_usage(db, user)
    monitors_used = db.scalar(select(func.count(Monitor.id)).where(Monitor.user_id == user.id)) or 0
    return UsageOut(
        period=datetime.now(timezone.utc).strftime("%Y-%m"),
        plan=plan.key,
        checks_used=record.checks_used if record else 0,
        checks_included=plan.max_checks_per_month,
        changes_detected=record.changes_detected if record else 0,
        monitors_used=monitors_used,
        monitors_allowed=plan.max_monitors,
        min_interval_seconds=plan.min_interval_seconds,
    )
