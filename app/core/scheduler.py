"""Background scheduler.

One tick does two things: run every monitor that is due, then drain the
delivery queue. Monitors are executed in a bounded thread pool so one slow
target cannot stall the rest.

The scheduler can run inside the API process (fine up to a few thousand
monitors) or as a separate `python -m app.worker` process. docker-compose runs
it separately so the API stays responsive.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.core.engine import claim_due_monitors, run_check
from app.core.notifier import process_pending_deliveries
from app.core.retention import sweep as retention_sweep
from app.db import session_scope
from app.logging_conf import get_logger

log = get_logger(__name__)

_tick_lock = threading.Lock()


def _run_one(monitor_id: int) -> None:
    """Each monitor gets its own session: a failure isolates to one monitor."""
    try:
        with session_scope() as db:
            from app.models import Monitor

            monitor = db.get(Monitor, monitor_id)
            if monitor is None or not monitor.is_active:
                return
            run_check(db, monitor)
    except Exception:
        log.exception("monitor_run_failed", monitor_id=monitor_id)


def tick() -> dict:
    """One scheduler pass. Safe to call directly from tests."""
    if not _tick_lock.acquire(blocking=False):
        log.debug("tick_skipped_overlap")
        return {"skipped": True}

    try:
        with session_scope() as db:
            due = claim_due_monitors(db, settings.check_batch_size)
            monitor_ids = [m.id for m in due]

        if monitor_ids:
            with ThreadPoolExecutor(max_workers=settings.worker_concurrency) as pool:
                list(pool.map(_run_one, monitor_ids))

        with session_scope() as db:
            delivered = process_pending_deliveries(db)

        if monitor_ids or delivered:
            log.info("tick_complete", monitors_checked=len(monitor_ids), deliveries_processed=delivered)
        return {"skipped": False, "monitors_checked": len(monitor_ids), "deliveries_processed": delivered}
    except Exception:
        log.exception("tick_failed")
        return {"skipped": False, "error": True}
    finally:
        _tick_lock.release()


def run_retention() -> dict:
    """Delete history past each plan's retention window."""
    try:
        with session_scope() as db:
            return retention_sweep(db)
    except Exception:
        log.exception("retention_sweep_failed")
        return {"error": True}


_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        tick,
        "interval",
        seconds=settings.scheduler_tick_seconds,
        id="watchpost_tick",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=settings.scheduler_tick_seconds * 3,
    )
    if settings.retention_enabled:
        _scheduler.add_job(
            run_retention,
            "interval",
            minutes=settings.retention_sweep_minutes,
            id="watchpost_retention",
            max_instances=1,
            coalesce=True,
        )
    _scheduler.start()
    log.info("scheduler_started", tick_seconds=settings.scheduler_tick_seconds)
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("scheduler_stopped")
    _scheduler = None
