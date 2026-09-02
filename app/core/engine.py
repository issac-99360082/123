"""The check loop: fetch -> extract -> compare -> record -> notify.

`run_check` is deliberately the only place that mutates a monitor's stored
content, so the "did it change?" decision has exactly one implementation and
is directly unit-testable without a scheduler or an HTTP server.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
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


# Fairness is done in the QUERY, per user. Widening a single global
# "ORDER BY next_check_at LIMIT n" cannot work: an account with more due
# monitors than the sample window fills it outright no matter how wide it is.
MAX_USERS_PER_TICK = 200


def _interleave_by_user(by_user: dict[int, list["Monitor"]]) -> list["Monitor"]:
    """Round-robin across owners, oldest-due first within each owner, so one
    account with a large backlog contributes one monitor per round instead of an
    unbroken run at the head of the list."""
    ordered: list[Monitor] = []
    queues = list(by_user.values())
    index = 0
    while queues:
        still_active = []
        for queue in queues:
            if index < len(queue):
                ordered.append(queue[index])
                still_active.append(queue)
        queues = still_active
        index += 1
    return ordered


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


def _recent_empty_streak(db: Session, monitor_id: int) -> int:
    """How many of the most recent checks in a row saw empty content.

    Derived from the check history rather than a counter column so it needs no
    schema migration; the project has no migration tooling yet (docs/DEPLOY.md).
    """
    recent = db.scalars(
        select(Check)
        .where(Check.monitor_id == monitor_id)
        .order_by(Check.started_at.desc(), Check.id.desc())
        .limit(10)
    ).all()
    streak = 0
    for check in recent:
        if check.error_type == "empty_content":
            streak += 1
        else:
            break
    return streak


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

    # --- transient-blank guard --------------------------------------------
    # An HTTP 200 with a body that extracts to nothing is far more often a site
    # mid-deploy, an interstitial, or a soft error page than a real edit. Taking
    # it at face value produces two false alerts per blip - one when the content
    # "disappears" and another when it "comes back". Measured: two blips of a
    # stable page produced four webhook notifications, none of them real.
    #
    # So an empty extraction that replaces non-empty content must be CONFIRMED by
    # consecutive observations before it is believed. A page that genuinely
    # empties still reports, just one interval later.
    if not new_content and monitor.last_content:
        confirmations = _recent_empty_streak(db, monitor.id) + 1
        if confirmations < settings.empty_content_confirmations:
            check.status = CheckStatus.error.value
            check.error_type = "empty_content"
            check.error_message = (
                f"Extracted content was empty while the previous observation was not "
                f"({confirmations}/{settings.empty_content_confirmations} confirmations). "
                "Treating as a transient blank response, not a change."
            )
            monitor.consecutive_failures += 1
            _schedule_next(monitor, failed=True)
            db.add(check)
            record_usage(db, monitor.user_id, checks=1)
            db.flush()
            log.info(
                "empty_content_unconfirmed",
                monitor_id=monitor.id, confirmations=confirmations,
                required=settings.empty_content_confirmations,
            )
            return check
        log.info("empty_content_confirmed", monitor_id=monitor.id, confirmations=confirmations)

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

    # Which accounts have work waiting. Cheap, and bounded per tick.
    due_user_ids = db.scalars(
        select(Monitor.user_id)
        .where(Monitor.is_active.is_(True))
        .where(Monitor.next_check_at <= now)
        .group_by(Monitor.user_id)
        .order_by(func.min(Monitor.next_check_at))
        .limit(MAX_USERS_PER_TICK)
    ).all()
    if not due_user_ids:
        return []

    # Pull each account's oldest-due monitors SEPARATELY, so a large backlog on
    # one account cannot displace another account's work before we ever see it.
    # Fetch a little more than each account's fair share, so the second pass has
    # material to top the batch up with once every account has been served.
    per_user_cap_rows = max(1, int(limit * settings.max_batch_share_per_user))
    per_user_fetch = min(
        limit, max(per_user_cap_rows * 2, -(-limit // len(due_user_ids)) + 1)
    )
    if len(due_user_ids) == 1:
        per_user_fetch = limit

    by_user: dict[int, list[Monitor]] = {}
    for user_id in due_user_ids:
        by_user[user_id] = db.scalars(
            select(Monitor)
            .where(Monitor.is_active.is_(True))
            .where(Monitor.next_check_at <= now)
            .where(Monitor.user_id == user_id)
            .order_by(Monitor.next_check_at)
            .limit(per_user_fetch)
        ).all()

    candidates = _interleave_by_user(by_user)

    claimed: list[Monitor] = []
    taken_per_user: dict[int, int] = {}

    # Fairness cap: ORDER BY next_check_at alone lets one account whose monitors
    # are all due fill the entire batch. Measured with 45 hanging monitors from
    # one user, a tick took 125s at production timeouts, leaving a second user's
    # 60s-interval monitors permanently late.
    #
    # Two passes, so fairness costs nothing when there is no contention:
    #   pass 1 honours the per-user cap;
    #   pass 2 fills any slots still free, ignoring the cap.
    # With one active user the batch still fills completely; with many, no
    # single user can crowd the others out.
    per_user_cap = max(1, int(limit * settings.max_batch_share_per_user))

    def _try_claim(monitor: Monitor, cap: int | None) -> bool:
        if cap is not None and taken_per_user.get(monitor.user_id, 0) >= cap:
            return False

        user = db.get(User, monitor.user_id)
        if user is None or not user.is_active:
            return False

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
            return False

        # A downgrade must not leave a monitor running faster than the plan allows.
        interval = max(monitor.interval_seconds, plan.min_interval_seconds)

        # Claim with a SHORT LEASE, not the full interval. run_check() sets the
        # real next_check_at when it finishes; if this process dies first the
        # lease expires in claim_lease_seconds and another worker retries,
        # instead of the check being silently skipped for a whole interval.
        lease_until = now + timedelta(seconds=min(settings.claim_lease_seconds, interval))
        result = db.execute(
            update(Monitor)
            .where(Monitor.id == monitor.id, Monitor.next_check_at <= now)
            .values(next_check_at=lease_until, interval_seconds=interval)
        )
        db.commit()

        if result.rowcount != 1:
            # Another worker got there first between our SELECT and UPDATE.
            log.debug("monitor_claim_lost", monitor_id=monitor.id)
            return False

        db.refresh(monitor)
        taken_per_user[monitor.user_id] = taken_per_user.get(monitor.user_id, 0) + 1
        claimed.append(monitor)
        return True

    remaining: list[Monitor] = []
    for monitor in candidates:
        if len(claimed) >= limit:
            break
        if not _try_claim(monitor, per_user_cap):
            remaining.append(monitor)

    # Any slots still free go to whoever is waiting, cap ignored - fairness must
    # not cost throughput when there is no contention.
    for monitor in remaining:
        if len(claimed) >= limit:
            break
        _try_claim(monitor, None)

    return claimed
