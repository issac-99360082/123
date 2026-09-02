"""Liveness and readiness probes."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app import __version__
from app.config import settings
from app.db import get_db
from app.models import Delivery, DeliveryStatus, Monitor

router = APIRouter(tags=["system"])


@router.get("/healthz")
def healthz() -> dict:
    """Liveness: the process is up. Never touches the database."""
    return {"status": "ok", "version": __version__, "time": datetime.now(timezone.utc).isoformat()}


@router.get("/readyz")
def readyz(response: Response, db: Session = Depends(get_db)) -> dict:
    """Readiness: dependencies are usable. Load balancers should gate on this."""
    checks: dict[str, str] = {}
    ok = True
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc.__class__.__name__}"
        ok = False

    checks["billing"] = "configured" if settings.billing_enabled else "disabled"
    checks["email"] = "configured" if settings.email_enabled else "disabled"
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if ok else "degraded", "checks": checks}


@router.get("/metrics")
def metrics(db: Session = Depends(get_db)) -> dict:
    """Lightweight operational counters (JSON, not Prometheus - enough for one VPS)."""
    now = datetime.now(timezone.utc)
    return {
        "time": now.isoformat(),
        "monitors_total": db.scalar(select(func.count(Monitor.id))) or 0,
        "monitors_active": db.scalar(
            select(func.count(Monitor.id)).where(Monitor.is_active.is_(True))
        ) or 0,
        "monitors_due": db.scalar(
            select(func.count(Monitor.id)).where(
                Monitor.is_active.is_(True), Monitor.next_check_at <= now
            )
        ) or 0,
        "deliveries_pending": db.scalar(
            select(func.count(Delivery.id)).where(Delivery.status == DeliveryStatus.pending.value)
        ) or 0,
        "deliveries_failed": db.scalar(
            select(func.count(Delivery.id)).where(Delivery.status == DeliveryStatus.failed.value)
        ) or 0,
    }
