"""The check loop: change detection, error handling, backoff and notification queueing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.core import engine as engine_module
from app.core.engine import claim_due_monitors, run_check
from app.core.fetcher import FetchError, FetchResult
from app.models import (
    CheckStatus,
    ChangeEvent,
    Delivery,
    DeliveryStatus,
    Monitor,
    MonthlyUsage,
    User,
)
from app.security import hash_password


@pytest.fixture
def user(db) -> User:
    user = User(email="engine@example.com", password_hash=hash_password("correct-horse-42"), plan="business")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def monitor(db, user) -> Monitor:
    monitor = Monitor(
        user_id=user.id,
        name="Price watch",
        url="https://example.com/pricing",
        extract_mode="css",
        extract_value=".price",
        interval_seconds=300,
        notify_webhook_url="https://hooks.example.com/wp",
        notify_webhook_secret="whsec_test",
        next_check_at=datetime.now(timezone.utc),
    )
    db.add(monitor)
    db.commit()
    db.refresh(monitor)
    return monitor


def fake_page(price: str) -> str:
    return f'<html><body><div class="price">{price}</div></body></html>'


def stub_fetch(monkeypatch, *, body: str = "", status_code: int = 200, error: FetchError | None = None):
    def _fetch(url, method="GET", headers=None, body_=None, **kwargs):
        if error is not None:
            raise error
        return FetchResult(
            url=url, status_code=status_code, headers={}, body=body,
            elapsed_ms=12.3, truncated=False,
        )

    monkeypatch.setattr(engine_module, "fetch", _fetch)


def test_first_check_records_baseline_without_firing_a_change(db, monitor, monkeypatch):
    stub_fetch(monkeypatch, body=fake_page("$19.00"))
    check = run_check(db, monitor)
    db.commit()

    assert check.status == CheckStatus.unchanged.value
    assert monitor.last_content == "$19.00"
    assert db.query(ChangeEvent).count() == 0, "a first observation is not a change"


def test_identical_content_does_not_fire(db, monitor, monkeypatch):
    stub_fetch(monkeypatch, body=fake_page("$19.00"))
    run_check(db, monitor)
    check = run_check(db, monitor)
    db.commit()

    assert check.status == CheckStatus.unchanged.value
    assert db.query(ChangeEvent).count() == 0


def test_changed_content_creates_event_and_queues_delivery(db, monitor, monkeypatch):
    stub_fetch(monkeypatch, body=fake_page("$19.00"))
    run_check(db, monitor)

    stub_fetch(monkeypatch, body=fake_page("$17.50"))
    check = run_check(db, monitor)
    db.commit()

    assert check.status == CheckStatus.changed.value
    event = db.query(ChangeEvent).one()
    assert event.diff["modified"] == [{"before": "$19.00", "after": "$17.50"}]
    assert event.old_hash != event.new_hash

    delivery = db.query(Delivery).one()
    assert delivery.channel == "webhook"
    assert delivery.target == "https://hooks.example.com/wp"
    assert delivery.status == DeliveryStatus.pending.value


def test_cosmetic_whitespace_change_does_not_fire(db, monitor, monkeypatch):
    stub_fetch(monkeypatch, body='<div class="price">$19.00</div>')
    run_check(db, monitor)
    stub_fetch(monkeypatch, body='<div class="price">\n   $19.00   \n</div>')
    check = run_check(db, monitor)
    db.commit()

    assert check.status == CheckStatus.unchanged.value
    assert db.query(ChangeEvent).count() == 0


def test_http_error_is_recorded_and_backs_off(db, monitor, monkeypatch):
    stub_fetch(monkeypatch, body="", status_code=503)
    before = monitor.next_check_at
    check = run_check(db, monitor)
    db.commit()

    assert check.status == CheckStatus.error.value
    assert check.error_type == "http_error"
    assert monitor.consecutive_failures == 1
    assert monitor.next_check_at > before + timedelta(seconds=monitor.interval_seconds - 1)


def test_network_error_is_typed_not_crashing(db, monitor, monkeypatch):
    stub_fetch(monkeypatch, error=FetchError("timeout", "Request timed out"))
    check = run_check(db, monitor)
    db.commit()
    assert check.status == CheckStatus.error.value
    assert check.error_type == "timeout"


def test_selector_miss_is_an_error_not_a_change(db, monitor, monkeypatch):
    stub_fetch(monkeypatch, body=fake_page("$19.00"))
    run_check(db, monitor)

    stub_fetch(monkeypatch, body="<html><body>maintenance</body></html>")
    check = run_check(db, monitor)
    db.commit()

    assert check.status == CheckStatus.error.value
    assert check.error_type == "selector_no_match"
    # The baseline must survive, or the page coming back would fire a false change.
    assert monitor.last_content == "$19.00"
    assert db.query(ChangeEvent).count() == 0


def test_monitor_auto_pauses_after_repeated_failures(db, monitor, monkeypatch):
    stub_fetch(monkeypatch, error=FetchError("dns_error", "nope"))
    for _ in range(settings.consecutive_failures_before_pause):
        run_check(db, monitor)
    db.commit()

    assert monitor.is_active is False
    assert "consecutive failures" in monitor.paused_reason


def test_recovery_resets_the_failure_counter(db, monitor, monkeypatch):
    stub_fetch(monkeypatch, error=FetchError("timeout", "slow"))
    run_check(db, monitor)
    assert monitor.consecutive_failures == 1

    stub_fetch(monkeypatch, body=fake_page("$19.00"))
    run_check(db, monitor)
    db.commit()
    assert monitor.consecutive_failures == 0


def test_usage_is_metered_per_check_and_change(db, monitor, user, monkeypatch):
    stub_fetch(monkeypatch, body=fake_page("$19.00"))
    run_check(db, monitor)
    stub_fetch(monkeypatch, body=fake_page("$17.50"))
    run_check(db, monitor)
    db.commit()

    usage = db.query(MonthlyUsage).filter(MonthlyUsage.user_id == user.id).one()
    assert usage.checks_used == 2
    assert usage.changes_detected == 1


def test_claim_skips_monitors_that_are_not_due(db, monitor):
    monitor.next_check_at = datetime.now(timezone.utc) + timedelta(hours=1)
    db.commit()
    assert claim_due_monitors(db, 10) == []


def test_claim_advances_next_check_so_a_second_worker_cannot_double_run(db, monitor):
    claimed = claim_due_monitors(db, 10)
    assert [m.id for m in claimed] == [monitor.id]
    assert claim_due_monitors(db, 10) == [], "already-claimed monitor must not be picked up twice"


def test_claim_enforces_the_plan_minimum_interval_after_a_downgrade(db, monitor, user):
    monitor.interval_seconds = 60
    user.plan = "free"  # free minimum is hourly
    db.commit()

    claim_due_monitors(db, 10)
    db.refresh(monitor)
    assert monitor.interval_seconds == 3600


def test_claim_skips_users_over_their_monthly_check_quota(db, monitor, user):
    user.plan = "free"
    db.add(
        MonthlyUsage(
            user_id=user.id,
            period=datetime.now(timezone.utc).strftime("%Y-%m"),
            checks_used=999_999,
            changes_detected=0,
        )
    )
    db.commit()
    assert claim_due_monitors(db, 10) == []
