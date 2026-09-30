"""Data retention.

The plans advertise 7 / 90 / 365 days of history, and the privacy policy
promises that older data is deleted. Before this module existed, `history_days`
only filtered read queries - nothing ever removed a row.

That is both a broken promise and a cost problem: one Business customer at the
advertised limits generates ~13 million check rows a month, roughly 1.9 GB, and
the unit-economics model assumes that storage does not accumulate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.logging_conf import get_logger
from app.models import ChangeEvent, Check, Delivery, DeliveryStatus, Monitor, User
from app.plans import get_plan

log = get_logger(__name__)

# Deliveries are kept a short fixed period after they settle; they are an
# operational audit trail, not customer-facing history.
DELIVERY_RETENTION_DAYS = 30


def _cutoffs(db: Session) -> dict[int, datetime]:
    """Per-monitor cutoff timestamp, derived from each owner's plan."""
    now = datetime.now(timezone.utc)
    rows = db.execute(select(Monitor.id, User.plan).join(User, Monitor.user_id == User.id)).all()
    return {
        monitor_id: now - timedelta(days=get_plan(plan).history_days)
        for monitor_id, plan in rows
    }


def sweep(db: Session, batch_size: int | None = None) -> dict[str, int]:
    """Delete history past each monitor's plan retention. Idempotent."""
    batch_size = batch_size or settings.retention_batch_size
    removed = {"checks": 0, "change_events": 0, "deliveries": 0}

    for monitor_id, cutoff in _cutoffs(db).items():
        stale_checks = db.scalars(
            select(Check.id)
            .where(Check.monitor_id == monitor_id, Check.started_at < cutoff)
            .limit(batch_size)
        ).all()
        if stale_checks:
            db.execute(delete(Check).where(Check.id.in_(stale_checks)))
            removed["checks"] += len(stale_checks)

        stale_events = db.scalars(
            select(ChangeEvent.id)
            .where(ChangeEvent.monitor_id == monitor_id, ChangeEvent.created_at < cutoff)
            .limit(batch_size)
        ).all()
        if stale_events:
            db.execute(delete(ChangeEvent).where(ChangeEvent.id.in_(stale_events)))
            removed["change_events"] += len(stale_events)

    delivery_cutoff = datetime.now(timezone.utc) - timedelta(days=DELIVERY_RETENTION_DAYS)
    stale_deliveries = db.scalars(
        select(Delivery.id)
        .where(
            Delivery.status.in_([DeliveryStatus.delivered.value, DeliveryStatus.failed.value]),
            Delivery.created_at < delivery_cutoff,
        )
        .limit(batch_size)
    ).all()
    if stale_deliveries:
        db.execute(delete(Delivery).where(Delivery.id.in_(stale_deliveries)))
        removed["deliveries"] += len(stale_deliveries)

    db.commit()
    if any(removed.values()):
        log.info("retention_sweep", **removed)
    return removed


def storage_stats(db: Session) -> dict[str, int]:
    return {
        "checks": db.scalar(select(func.count(Check.id))) or 0,
        "change_events": db.scalar(select(func.count(ChangeEvent.id))) or 0,
        "deliveries": db.scalar(select(func.count(Delivery.id))) or 0,
    }
