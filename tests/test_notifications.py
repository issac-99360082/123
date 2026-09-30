"""Webhook delivery: payload shape, signing, retries and the failure ceiling."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import settings
from app.core import notifier as notifier_module
from app.core.notifier import (
    attempt_delivery,
    build_payload,
    process_pending_deliveries,
    queue_deliveries,
)
from app.models import ChangeEvent, Delivery, DeliveryStatus, Monitor, User
from app.security import hash_password, sign_webhook


@pytest.fixture
def monitor(db) -> Monitor:
    user = User(email="n@example.com", password_hash=hash_password("correct-horse-42"), plan="pro")
    db.add(user)
    db.commit()
    monitor = Monitor(
        user_id=user.id,
        name="Price watch",
        url="https://example.com/pricing",
        extract_mode="css",
        extract_value=".price",
        notify_webhook_url="https://example.com/hooks/wp",
        notify_webhook_secret="whsec_test_secret",
        next_check_at=datetime.now(timezone.utc),
    )
    db.add(monitor)
    db.commit()
    db.refresh(monitor)
    return monitor


@pytest.fixture
def event(db, monitor) -> ChangeEvent:
    event = ChangeEvent(
        monitor_id=monitor.id,
        old_hash="aaa",
        new_hash="bbb",
        diff={"lines_modified": 1, "modified": [{"before": "$19.00", "after": "$17.50"}]},
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def test_payload_contains_the_documented_contract(monitor, event):
    payload = build_payload(monitor, event)
    assert payload["event"] == "monitor.changed"
    assert payload["event_id"] == f"evt_{event.id}"
    assert payload["monitor"]["id"] == monitor.id
    assert payload["monitor"]["url"] == monitor.url
    assert payload["change"]["previous_hash"] == "aaa"
    assert payload["change"]["current_hash"] == "bbb"
    assert payload["change"]["modified"] == [{"before": "$19.00", "after": "$17.50"}]
    # The secret must never appear in what we send.
    assert "whsec_test_secret" not in json.dumps(payload)


def test_queue_creates_one_delivery_per_channel(db, monitor, event):
    monitor.notify_email = "alerts@example.com"
    db.commit()
    deliveries = queue_deliveries(db, monitor, event)
    db.commit()
    assert {d.channel for d in deliveries} == {"webhook", "email"}
    assert all(d.status == DeliveryStatus.pending.value for d in deliveries)


def test_successful_delivery_is_signed_and_marked_delivered(db, monitor, event, monkeypatch):
    captured = {}

    def fake_post(url, content=None, headers=None, **kwargs):
        captured["url"] = url
        captured["body"] = content
        captured["headers"] = headers
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(notifier_module.httpx, "post", fake_post)

    delivery = queue_deliveries(db, monitor, event)[0]
    db.commit()
    assert attempt_delivery(db, delivery) is True
    db.commit()

    assert delivery.status == DeliveryStatus.delivered.value
    assert delivery.response_status == 200 and delivery.attempts == 1

    signature = captured["headers"]["X-Watchpost-Signature"]
    timestamp = captured["headers"]["X-Watchpost-Timestamp"]
    version, digest = signature.split(",")
    assert version == f"t={timestamp}"
    expected = sign_webhook("whsec_test_secret", timestamp, captured["body"])
    assert digest == f"v1={expected}", "receiver must be able to verify the signature"


def test_receiver_error_schedules_a_retry_with_backoff(db, monitor, event, monkeypatch):
    monkeypatch.setattr(
        notifier_module.httpx,
        "post",
        lambda url, **kwargs: httpx.Response(500, request=httpx.Request("POST", url)),
    )
    delivery = queue_deliveries(db, monitor, event)[0]
    db.commit()

    assert attempt_delivery(db, delivery) is False
    db.commit()
    assert delivery.status == DeliveryStatus.pending.value
    assert delivery.attempts == 1
    assert delivery.next_attempt_at > datetime.now(timezone.utc)
    assert "500" in delivery.last_error


def test_delivery_gives_up_after_the_attempt_ceiling(db, monitor, event, monkeypatch):
    monkeypatch.setattr(
        notifier_module.httpx,
        "post",
        lambda url, **kwargs: httpx.Response(500, request=httpx.Request("POST", url)),
    )
    delivery = queue_deliveries(db, monitor, event)[0]
    db.commit()

    for _ in range(settings.webhook_max_attempts):
        delivery.next_attempt_at = datetime.now(timezone.utc)
        attempt_delivery(db, delivery)
    db.commit()

    assert delivery.attempts == settings.webhook_max_attempts
    assert delivery.status == DeliveryStatus.failed.value


def test_network_exception_is_caught_not_propagated(db, monitor, event, monkeypatch):
    def boom(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(notifier_module.httpx, "post", boom)
    delivery = queue_deliveries(db, monitor, event)[0]
    db.commit()

    assert attempt_delivery(db, delivery) is False
    assert "ConnectError" in delivery.last_error


def test_webhook_target_pointing_at_localhost_is_refused(db, monitor, event):
    monitor.notify_webhook_url = "http://127.0.0.1:9999/hook"
    db.commit()
    delivery = queue_deliveries(db, monitor, event)[0]
    db.commit()

    assert attempt_delivery(db, delivery) is False
    assert "not a public address" in delivery.last_error


def test_unsigned_when_no_secret_is_set(db, monitor, event, monkeypatch):
    captured = {}

    def fake_post(url, content=None, headers=None, **kwargs):
        captured["headers"] = headers
        return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(notifier_module.httpx, "post", fake_post)
    monitor.notify_webhook_secret = None
    db.commit()

    delivery = queue_deliveries(db, monitor, event)[0]
    db.commit()
    assert attempt_delivery(db, delivery) is True
    assert "X-Watchpost-Signature" not in captured["headers"]


def test_queue_processor_only_picks_up_due_deliveries(db, monitor, event, monkeypatch):
    monkeypatch.setattr(
        notifier_module.httpx,
        "post",
        lambda url, **kwargs: httpx.Response(200, request=httpx.Request("POST", url)),
    )
    due = queue_deliveries(db, monitor, event)[0]
    not_due = Delivery(
        change_event_id=event.id,
        channel="webhook",
        target="https://example.com/hooks/later",
        next_attempt_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add(not_due)
    db.commit()

    assert process_pending_deliveries(db) == 1
    db.refresh(due)
    db.refresh(not_due)
    assert due.status == DeliveryStatus.delivered.value
    assert not_due.status == DeliveryStatus.pending.value


def test_email_delivery_fails_cleanly_when_smtp_is_unconfigured(db, monitor, event):
    monitor.notify_webhook_url = None
    monitor.notify_email = "alerts@example.com"
    db.commit()
    delivery = queue_deliveries(db, monitor, event)[0]
    db.commit()

    assert attempt_delivery(db, delivery) is False
    assert "SMTP is not configured" in delivery.last_error
