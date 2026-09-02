"""Delivery of change notifications.

Webhook is the primary channel and needs no third-party account: we POST a
signed JSON payload to a URL the user controls. Email is optional and only
works when SMTP settings are supplied.

Delivery is persisted as a `Delivery` row and retried with exponential backoff,
so a receiver that is briefly down does not lose the event.
"""

from __future__ import annotations

import json
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.differ import summarise
from app.core.fetcher import validate_target_url, FetchError
from app.logging_conf import get_logger
from app.models import ChangeEvent, Delivery, DeliveryStatus, Monitor
from app.security import sign_webhook

log = get_logger(__name__)

# Attempt n is retried after BACKOFF_SCHEDULE[n] seconds.
BACKOFF_SCHEDULE = [60, 300, 1800, 7200, 21600]


def build_payload(monitor: Monitor, event: ChangeEvent) -> dict:
    """The public webhook payload. Documented in docs/API.md - treat as a contract."""
    return {
        "event": "monitor.changed",
        "event_id": f"evt_{event.id}",
        "created_at": (event.created_at or datetime.now(timezone.utc)).isoformat(),
        "monitor": {
            "id": monitor.id,
            "name": monitor.name,
            "url": monitor.url,
            "extract_mode": monitor.extract_mode,
            "extract_value": monitor.extract_value,
            "interval_seconds": monitor.interval_seconds,
        },
        "change": {
            "previous_hash": event.old_hash,
            "current_hash": event.new_hash,
            **(event.diff or {}),
        },
    }


def queue_deliveries(db: Session, monitor: Monitor, event: ChangeEvent) -> list[Delivery]:
    """Create pending delivery rows for every channel this monitor uses."""
    deliveries: list[Delivery] = []
    now = datetime.now(timezone.utc)

    if monitor.notify_webhook_url:
        deliveries.append(
            Delivery(
                change_event_id=event.id,
                channel="webhook",
                target=monitor.notify_webhook_url,
                next_attempt_at=now,
            )
        )
    if monitor.notify_email:
        deliveries.append(
            Delivery(
                change_event_id=event.id,
                channel="email",
                target=monitor.notify_email,
                next_attempt_at=now,
            )
        )
    for delivery in deliveries:
        db.add(delivery)
    db.flush()
    return deliveries


def _send_webhook(monitor: Monitor, payload: dict, target: str) -> int:
    # The user's webhook URL goes through the same SSRF guard as monitored URLs.
    safe_url = validate_target_url(target)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))

    headers = {
        "Content-Type": "application/json",
        "User-Agent": settings.user_agent,
        "X-Watchpost-Event": payload.get("event", "monitor.changed"),
        "X-Watchpost-Timestamp": timestamp,
        "X-Watchpost-Delivery": payload.get("event_id", ""),
    }
    if monitor.notify_webhook_secret:
        headers["X-Watchpost-Signature"] = (
            f"t={timestamp},v1={sign_webhook(monitor.notify_webhook_secret, timestamp, body)}"
        )

    response = httpx.post(
        safe_url,
        content=body,
        headers=headers,
        timeout=settings.webhook_timeout_seconds,
        follow_redirects=False,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Receiver returned HTTP {response.status_code}")
    return response.status_code


def _send_email(monitor: Monitor, payload: dict, target: str) -> int:
    if not settings.email_enabled:
        raise RuntimeError("SMTP is not configured (set SMTP_HOST); email delivery is unavailable.")

    change = payload.get("change", {})
    summary = summarise(change, monitor.name)

    message = EmailMessage()
    message["Subject"] = f"[Watchpost] {summary}"
    message["From"] = settings.notification_from_email
    message["To"] = target
    message.set_content(
        f"{summary}\n\n"
        f"Monitor: {monitor.name}\n"
        f"URL: {monitor.url}\n"
        f"Detected: {payload.get('created_at')}\n\n"
        f"--- diff ---\n{change.get('unified_diff') or '(no line diff available)'}\n\n"
        f"Manage this monitor: {settings.public_base_url}/app\n"
    )

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
        if settings.smtp_use_tls:
            server.starttls()
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password or "")
        server.send_message(message)
    return 250


def attempt_delivery(db: Session, delivery: Delivery) -> bool:
    """Try one delivery. Returns True on success. Schedules a retry on failure."""
    event = db.get(ChangeEvent, delivery.change_event_id)
    if event is None:
        delivery.status = DeliveryStatus.failed.value
        delivery.last_error = "change event no longer exists"
        return False
    monitor = db.get(Monitor, event.monitor_id)
    if monitor is None:
        delivery.status = DeliveryStatus.failed.value
        delivery.last_error = "monitor no longer exists"
        return False

    payload = build_payload(monitor, event)
    delivery.attempts += 1

    try:
        if delivery.channel == "webhook":
            status_code = _send_webhook(monitor, payload, delivery.target)
        elif delivery.channel == "email":
            status_code = _send_email(monitor, payload, delivery.target)
        else:
            raise RuntimeError(f"unknown channel {delivery.channel!r}")
    except (httpx.HTTPError, FetchError, smtplib.SMTPException, RuntimeError, OSError) as exc:
        delivery.last_error = f"{exc.__class__.__name__}: {exc}"[:1024]
        if delivery.attempts >= settings.webhook_max_attempts:
            delivery.status = DeliveryStatus.failed.value
            log.warning(
                "delivery_failed_permanently",
                delivery_id=delivery.id, channel=delivery.channel,
                monitor_id=monitor.id, attempts=delivery.attempts, error=delivery.last_error,
            )
        else:
            backoff = BACKOFF_SCHEDULE[min(delivery.attempts - 1, len(BACKOFF_SCHEDULE) - 1)]
            delivery.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=backoff)
            log.info(
                "delivery_retry_scheduled",
                delivery_id=delivery.id, attempts=delivery.attempts,
                retry_in_s=backoff, error=delivery.last_error,
            )
        return False

    delivery.status = DeliveryStatus.delivered.value
    delivery.response_status = status_code
    delivery.delivered_at = datetime.now(timezone.utc)
    delivery.last_error = None
    log.info(
        "delivery_succeeded",
        delivery_id=delivery.id, channel=delivery.channel,
        monitor_id=monitor.id, attempts=delivery.attempts,
    )
    return True


def process_pending_deliveries(db: Session, limit: int = 50) -> int:
    """Drain the delivery queue. Called on every scheduler tick."""
    now = datetime.now(timezone.utc)
    pending = db.scalars(
        select(Delivery)
        .where(Delivery.status == DeliveryStatus.pending.value)
        .where(Delivery.next_attempt_at <= now)
        .order_by(Delivery.next_attempt_at)
        .limit(limit)
    ).all()

    processed = 0
    for delivery in pending:
        try:
            attempt_delivery(db, delivery)
        except Exception:
            log.exception("delivery_attempt_crashed", delivery_id=delivery.id)
            delivery.status = DeliveryStatus.failed.value
            delivery.last_error = "internal error during delivery"
        processed += 1
    db.commit()
    return processed
