"""The check loop: fetch -> extract -> compare -> record -> notify.

`run_check` is deliberately the only place that mutates a monitor's stored
content, so the "did it change?" decision has exactly one implementation and
is directly unit-testable without a scheduler or an HTTP server.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.core.differ import build_diff, summarise
from app.core.extractor import ExtractionError, content_hash, extract
from app.core.fetcher import FetchError, fetch
from app.core.notifier import queue_deliveries
from app.logging_conf import get_logger
from app.models import (
    Check,
    ChangeEvent,
    CheckStatus,
    Monitor,
    MonthlyUsage,
    User,
)
from app.plans import get_plan

log = get_logger(__name__)


def _period(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def record_usage(db: Session, user_id: int, *, checks: int = 0, changes: int = 0) -> MonthlyUsage:
    period = _period()
    usage = db.scalar(
        select(MonthlyUsage).where(
            MonthlyUsage.user_id == user_id, MonthlyUsage.period == period
        )
    )
    if usage is None:
        usage = MonthlyUsage(user_id=user_id, period=period, checks_used=0, changes_detected=0)
        db.add(usage)
        db.flush()
    usage.checks_used += checks
    usage.changes_detected += changes
    return usage


def _schedule_next(monitor: Monitor, *, failed: bool) -> None:
    """Advance next_check_at, backing off after consecutive failures.

    Backoff protects both us and the target site: a URL that 404s does not get
    hit at full rate forever.
    """
    interval = monitor.interval_seconds
    if failed:
        interval = min(interval * min(2 ** min(monitor.consecutive_failures, 6), 32), 86_400)
    monitor.next_check_at = datetime.now(timezone.utc) + timedelta(seconds=interval)


def run_check(db: Session, monitor: Monitor, *, notify: bool = True) -> Check:
    """Execute one check for one monitor and persist the outcome.

    Always returns a Check row - failures are data, not exceptions, because a
    404 on a monitored page is a normal operating condition we must record.
    """
    started = datetime.now(timezone.utc)
    check = Check(monitor_id=monitor.id, started_at=started, status=CheckStatus.error.value)
    error_kind: str | None = None
    error_message: str | None = None
    new_content: str | None = None

    try:
        result = fetch(
            monitor.url,
            method=monitor.method,
            headers=monitor.request_headers,
            body=monitor.request_body,
        )
        check.http_status = result.status_code
        check.duration_ms = result.elapsed_ms
        monitor.last_status_code = result.status_code

        if result.status_code >= 400:
            error_kind = "http_error"
            error_message = f"Target returned HTTP {result.status_code}"
        else:
            new_content = extract(
                result.body,
                mode=monitor.extract_mode,
                selector=monitor.extract_value,
                ignore_patterns=monitor.ignore_patterns,
            )
    except FetchError as exc:
        error_kind, error_message = exc.kind, exc.message
    except ExtractionError as exc:
        error_kind, error_message = exc.kind, exc.message
    except Exception as exc:  # never let one bad monitor kill the worker
        log.exception("check_crashed", monitor_id=monitor.id)
        error_kind, error_message = "internal_error", f"{exc.__class__.__name__}: {exc}"

    monitor.last_checked_at = started

    # --- failure path -----------------------------------------------------
    if new_content is None:
        check.status = CheckStatus.error.value
        check.error_type = error_kind
        check.error_message = (error_message or "")[:1024]
        monitor.consecutive_failures += 1
        if monitor.consecutive_failures >= settings.consecutive_failures_before_pause:
            monitor.is_active = False
            monitor.paused_reason = (
                f"Paused automatically after {monitor.consecutive_failures} consecutive "
                f"failures (last error: {error_kind})."
            )
            log.warning(
                "monitor_auto_paused",
                monitor_id=monitor.id, failures=monitor.consecutive_failures, error=error_kind,
            )
        _schedule_next(monitor, failed=True)
        db.add(check)
        record_usage(db, monitor.user_id, checks=1)
        db.flush()
        log.info("check_error", monitor_id=monitor.id, error_type=error_kind, message=error_message)
        return check

    # --- success path -----------------------------------------------------
    monitor.consecutive_failures = 0
    new_hash = content_hash(new_content)
    check.content_hash = new_hash
    previous_hash = monitor.last_content_hash
    previous_content = monitor.last_content

    changed = previous_hash is not None and previous_hash != new_hash
    check.status = CheckStatus.changed.value if changed else CheckStatus.unchanged.value

    monitor.last_content_hash = new_hash
    monitor.last_content = new_content
    _schedule_next(monitor, failed=False)
    db.add(check)
    db.flush()

    if changed:
        monitor.last_changed_at = started
        diff = build_diff(previous_content, new_content)
        event = ChangeEvent(
            monitor_id=monitor.id,
            check_id=check.id,
            old_hash=previous_hash,
            new_hash=new_hash,
            diff=diff,
        )
        db.add(event)
        db.flush()
        if notify:
            queue_deliveries(db, monitor, event)
        record_usage(db, monitor.user_id, checks=1, changes=1)
        log.info(
            "change_detected",
            monitor_id=monitor.id, event_id=event.id, summary=summarise(diff, monitor.name),
        )
    else:
        record_usage(db, monitor.user_id, checks=1)
        log.debug("check_unchanged", monitor_id=monitor.id, hash=new_hash[:12])

    db.flush()
    return check


def claim_due_monitors(db: Session, limit: int) -> list[Monitor]:
    """Pick up active monitors that are due, respecting each owner's plan.

    Claiming is an ATOMIC conditional update:

        UPDATE monitors SET next_check_at = <now + interval>
        WHERE id = :id AND next_check_at <= :now

    and the monitor is taken only when that statement reports one changed row.
    A plain read-then-write here is a real bug, not a theoretical one: with four
    worker processes it was measured handing every due monitor to all four,
    producing four fetches of the target site, four sets of webhooks, and four
    times the compute cost per check.
    """
    now = datetime.now(timezone.utc)
    candidates = db.scalars(
        select(Monitor)
        .where(Monitor.is_active.is_(True))
        .where(Monitor.next_check_at <= now)
        .order_by(Monitor.next_check_at)
        .limit(limit)
    ).all()

    claimed: list[Monitor] = []
    for monitor in candidates:
        user = db.get(User, monitor.user_id)
        if user is None or not user.is_active:
            continue

        plan = get_plan(user.plan)
        usage = db.scalar(
            select(MonthlyUsage).where(
                MonthlyUsage.user_id == user.id, MonthlyUsage.period == _period(now)
            )
        )
        if usage and usage.checks_used >= plan.max_checks_per_month:
            db.execute(
                update(Monitor)
                .where(Monitor.id == monitor.id, Monitor.next_check_at <= now)
                .values(next_check_at=now + timedelta(hours=6))
            )
            db.commit()
            log.info("monitor_skipped_quota", monitor_id=monitor.id, user_id=user.id, plan=plan.key)
            continue

        # A downgrade must not leave a monitor running faster than the plan allows.
        interval = max(monitor.interval_seconds, plan.min_interval_seconds)

        result = db.execute(
            update(Monitor)
            .where(Monitor.id == monitor.id, Monitor.next_check_at <= now)
            .values(next_check_at=now + timedelta(seconds=interval), interval_seconds=interval)
        )
        db.commit()

        if result.rowcount == 1:
            db.refresh(monitor)
            claimed.append(monitor)
        else:
            # Another worker got there first between our SELECT and UPDATE.
            log.debug("monitor_claim_lost", monitor_id=monitor.id)

    return claimed
