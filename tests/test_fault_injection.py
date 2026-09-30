"""Regression tests for AUDIT_02_FAULT_INJECTION findings.

Each test pins a fault that was reproduced against a live server or a real
process before it was fixed. They are deliberately fault-shaped rather than
feature-shaped: the question is always "what does the system do when this goes
wrong", not "does the happy path work".
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.core.engine import claim_due_monitors, run_check
from app.core.fetcher import FetchError, fetch
from app.models import ChangeEvent, CheckStatus, Delivery, Monitor, User
from app.security import hash_password

@pytest.fixture
def owner(db) -> User:
    user = User(
        email="fault@example.com", password_hash=hash_password("correct-horse-42"), plan="business"
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_monitor(db, user, url, **kw) -> Monitor:
    monitor = Monitor(
        user_id=user.id,
        name=kw.pop("name", "m"),
        url=url,
        extract_mode=kw.pop("extract_mode", "text"),
        extract_value=kw.pop("extract_value", None),
        interval_seconds=kw.pop("interval_seconds", 3600),
        notify_webhook_url="https://example.com/hook",
        next_check_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(monitor)
    db.commit()
    db.refresh(monitor)
    return monitor


# ---------------------------------------------------------------------------
# CRITICAL - slow-loris: the deadline must apply while the body streams
# ---------------------------------------------------------------------------

def test_trickling_site_cannot_hold_a_worker_past_the_deadline(site, monkeypatch):
    """Measured before the fix: 60s against a 12s deadline. httpx timeouts are
    per socket operation, so a server that sends one small chunk just inside the
    timeout, forever, never trips them."""
    monkeypatch.setattr(settings, "fetch_timeout_seconds", 5.0)
    monkeypatch.setattr(settings, "fetch_total_deadline_seconds", 3.0)

    started = time.perf_counter()
    with pytest.raises(FetchError) as exc:
        fetch(f"{site.url}/slow", respect_robots=False, throttle=False)
    elapsed = time.perf_counter() - started

    assert exc.value.kind == "deadline_exceeded"
    assert elapsed < 8.0, f"a trickling site held the worker for {elapsed:.1f}s"


def test_decompression_bomb_stays_bounded(site, monkeypatch):
    monkeypatch.setattr(settings, "fetch_total_deadline_seconds", 30.0)
    result = fetch(f"{site.url}/bomb", respect_robots=False, throttle=False)
    assert result.truncated is True
    assert len(result.body) <= settings.fetch_max_bytes


def test_dns_failure_is_classified(monkeypatch):
    monkeypatch.setattr(settings, "allow_private_network_targets", True)
    with pytest.raises(FetchError) as exc:
        fetch("http://no-such-host-9f3a2b41.invalid/x", respect_robots=False, throttle=False)
    assert exc.value.kind == "dns_error"


def test_tls_failure_is_classified(site):
    """HTTPS pointed at a plain-HTTP listener: a handshake failure, not a
    refused connection. These used to collapse into one opaque 'network_error'."""
    https_url = site.url.replace("http://", "https://")
    with pytest.raises(FetchError) as exc:
        fetch(https_url, respect_robots=False, throttle=False)
    assert exc.value.kind == "tls_error"


def test_connection_refused_is_classified(monkeypatch):
    import socket

    monkeypatch.setattr(settings, "allow_private_network_targets", True)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(FetchError) as exc:
        fetch(f"http://127.0.0.1:{port}/x", respect_robots=False, throttle=False)
    assert exc.value.kind == "connection_refused"


# ---------------------------------------------------------------------------
# HIGH - a claimed check must not be lost when the worker dies
# ---------------------------------------------------------------------------

def test_claim_is_a_short_lease_not_a_full_reschedule(db, owner):
    """Before the fix the claim pushed next_check_at forward by a whole interval,
    so a worker that died mid-check silently skipped that cycle: for a daily
    monitor, a crash lost a full day with no Check row and no error."""
    monitor = make_monitor(db, owner, "https://example.com/p", interval_seconds=86_400)

    claimed = claim_due_monitors(db, 10)
    assert [m.id for m in claimed] == [monitor.id]

    db.expire_all()
    monitor = db.get(Monitor, monitor.id)
    wait = (monitor.next_check_at - datetime.now(timezone.utc)).total_seconds()
    assert wait <= settings.claim_lease_seconds + 5, (
        f"a crashed worker would strand this monitor for {wait / 3600:.1f}h"
    )

    # After the lease expires the work is retried rather than lost.
    db.query(Monitor).filter(Monitor.id == monitor.id).update(
        {"next_check_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
    )
    db.commit()
    assert [m.id for m in claim_due_monitors(db, 10)] == [monitor.id]


def test_completed_check_reschedules_at_the_full_interval(db, owner, site):
    """The lease must not become the real cadence for healthy monitors."""
    monitor = make_monitor(db, owner, f"{site.url}/", interval_seconds=3600)
    run_check(db, monitor)
    db.commit()
    wait = (monitor.next_check_at - datetime.now(timezone.utc)).total_seconds()
    assert wait > settings.claim_lease_seconds


# ---------------------------------------------------------------------------
# HIGH - transient blank pages must not look like changes
# ---------------------------------------------------------------------------

def test_transient_blank_page_produces_no_false_notification(db, owner, site):
    """Measured before the fix: two blips of an unchanged page produced four
    webhook notifications, none of them real."""
    monitor = make_monitor(db, owner, f"{site.url}/")
    run_check(db, monitor)          # baseline
    db.commit()
    baseline = monitor.last_content

    for path in ("/empty", "/", "/empty", "/"):
        monitor.url = f"{site.url}{path}"
        db.commit()
        run_check(db, monitor)
        db.commit()

    assert db.query(ChangeEvent).filter(ChangeEvent.monitor_id == monitor.id).count() == 0
    assert db.query(Delivery).count() == 0
    assert monitor.last_content == baseline, "the baseline must survive a blank blip"


def test_a_genuinely_emptied_page_is_still_reported(db, owner, site):
    """The blank guard must delay a real change, never suppress it."""
    monitor = make_monitor(db, owner, f"{site.url}/")
    run_check(db, monitor)
    db.commit()

    monitor.url = f"{site.url}/empty"
    db.commit()
    statuses = []
    for _ in range(settings.empty_content_confirmations + 1):
        check = run_check(db, monitor)
        db.commit()
        statuses.append(check.status)

    assert CheckStatus.changed.value in statuses, "a real emptying was never reported"
    assert db.query(ChangeEvent).filter(ChangeEvent.monitor_id == monitor.id).count() == 1


# ---------------------------------------------------------------------------
# HIGH - one account must not crowd others out of the batch
# ---------------------------------------------------------------------------

def test_one_user_cannot_monopolise_the_batch(db):
    """Measured before the fix: a user with 200 due monitors took all 20 batch
    slots and three other users got nothing, so their checks ran permanently
    late because of someone else's monitors."""
    users = {}
    for name, count in (("hog", 200), ("alice", 3), ("bob", 3), ("carol", 3)):
        user = User(
            email=f"{name}@example.com",
            password_hash=hash_password("correct-horse-42"),
            plan="business",
        )
        db.add(user)
        db.commit()
        users[user.id] = name
        for i in range(count):
            make_monitor(db, user, "https://example.com/p", name=f"{name}{i}", interval_seconds=60)

    claimed = claim_due_monitors(db, 20)
    by_name: dict[str, int] = {}
    for monitor in claimed:
        by_name[users[monitor.user_id]] = by_name.get(users[monitor.user_id], 0) + 1

    for small in ("alice", "bob", "carol"):
        assert by_name.get(small, 0) == 3, f"{small} was crowded out: {by_name}"
    assert by_name.get("hog", 0) < 20


def test_a_single_user_still_gets_the_whole_batch(db, owner):
    """Fairness must cost nothing when there is no contention."""
    for i in range(60):
        make_monitor(db, owner, "https://example.com/p", name=f"solo{i}", interval_seconds=60)
    assert len(claim_due_monitors(db, 50)) == 50


# ---------------------------------------------------------------------------
# HIGH - Stripe delivers out of order and concurrently
# ---------------------------------------------------------------------------

@pytest.fixture
def stripe_ready(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_fake")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_fake")
    monkeypatch.setattr(settings, "stripe_price_pro", "price_pro_123")
    monkeypatch.setattr(settings, "stripe_price_business", "price_biz_456")


def _subscription(user_id: int, price: str, status: str = "active") -> dict:
    return {
        "id": "sub_fault",
        "status": status,
        "customer": "cus_fault",
        "metadata": {"watchpost_user_id": str(user_id)},
        "current_period_end": 1_900_000_000,
        "items": {"data": [{"price": {"id": price}}]},
    }


def _deliver(client, monkeypatch, event: dict):
    from app.api import billing as billing_module

    monkeypatch.setattr(
        billing_module.stripe.Webhook, "construct_event", staticmethod(lambda *a, **k: event)
    )
    return client.post(
        "/v1/billing/webhook", content=b"{}", headers={"Stripe-Signature": "t=1,v1=x"}
    )


def test_a_stale_event_cannot_resurrect_a_cancelled_subscription(
    client, account, db, stripe_ready, monkeypatch
):
    """Stripe does not guarantee ordering. Measured before the fix: a late
    `subscription.updated` re-upgraded a cancelled account to Pro, which then
    kept paid features indefinitely with no active subscription."""
    user = db.query(User).filter(User.email == "dev@example.com").one()

    _deliver(client, monkeypatch, {
        "id": "evt_cancel", "type": "customer.subscription.deleted", "created": 2000,
        "data": {"object": _subscription(user.id, "price_pro_123", "canceled")},
    })
    db.expire_all()
    assert db.query(User).filter(User.id == user.id).one().plan == "free"

    # An OLDER event, delivered late.
    _deliver(client, monkeypatch, {
        "id": "evt_stale", "type": "customer.subscription.updated", "created": 1000,
        "data": {"object": _subscription(user.id, "price_pro_123", "active")},
    })
    db.expire_all()
    assert db.query(User).filter(User.id == user.id).one().plan == "free", (
        "a cancelled customer was silently re-upgraded by an out-of-order event"
    )


def test_a_newer_event_still_applies(client, account, db, stripe_ready, monkeypatch):
    """The ordering guard must not block legitimate later events."""
    user = db.query(User).filter(User.email == "dev@example.com").one()
    for eid, created, price in (
        ("evt_1", 1000, "price_pro_123"),
        ("evt_2", 2000, "price_biz_456"),
    ):
        _deliver(client, monkeypatch, {
            "id": eid, "type": "customer.subscription.updated", "created": created,
            "data": {"object": _subscription(user.id, price)},
        })
    db.expire_all()
    assert db.query(User).filter(User.id == user.id).one().plan == "business"


def test_concurrent_webhook_redelivery_never_returns_500(db, stripe_ready, monkeypatch):
    """Measured before the fix: 6 of 8 concurrent webhooks returned 500, because
    the StripeEvent commit sat outside the error handling and was the one
    unguarded write on the path. Stripe retries 500s, so contention turned into
    a redelivery storm."""
    import threading

    from fastapi.testclient import TestClient

    from app.api import billing as billing_module
    from app.main import app
    from app.security import hash_password

    user = User(
        email="concurrent@example.com",
        password_hash=hash_password("correct-horse-42"),
        plan="free",
    )
    db.add(user)
    db.commit()

    statuses: list[int] = []
    lock = threading.Lock()

    def deliver(i: int):
        event = {
            "id": f"evt_conc_{i}",
            "type": "customer.subscription.updated",
            "created": 7000 + i,
            "data": {"object": _subscription(user.id, "price_pro_123")},
        }
        billing_module.stripe.Webhook.construct_event = staticmethod(lambda *a, **k: event)
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.post(
                "/v1/billing/webhook", content=b"{}", headers={"Stripe-Signature": "t=1,v1=x"}
            )
        with lock:
            statuses.append(r.status_code)

    threads = [threading.Thread(target=deliver, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert 500 not in statuses, f"concurrent redelivery produced 500s: {statuses}"


# ---------------------------------------------------------------------------
# HIGH - the shipped configuration must be able to deliver the plans sold
# ---------------------------------------------------------------------------

def test_configured_capacity_can_serve_the_largest_plan():
    """Sustained capacity is check_batch_size / scheduler_tick_seconds and
    nothing else. The original defaults gave 2.5 checks/s while a single
    Business customer at the advertised limits needs 5 checks/s, so the pricing
    page promised twice what the shipped configuration could deliver."""
    report = settings.capacity_report()
    assert report["can_serve_one_largest_plan_customer"], (
        f"configuration sustains {report['sustained_checks_per_second']} checks/s but the "
        f"{report['largest_plan']} plan alone needs "
        f"{report['largest_plan_requires_checks_per_second']}"
    )


def test_capacity_report_detects_an_under_provisioned_configuration(monkeypatch):
    """The startup warning must actually fire when someone lowers the batch size."""
    monkeypatch.setattr(settings, "check_batch_size", 10)
    monkeypatch.setattr(settings, "scheduler_tick_seconds", 60)
    assert settings.capacity_report()["can_serve_one_largest_plan_customer"] is False
