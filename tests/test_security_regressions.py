"""Regression tests for findings from the production-readiness audits.

Each test here corresponds to a confirmed, exploited vulnerability. They exist
so a future refactor cannot quietly reopen one.
"""

from __future__ import annotations

import time

import pytest

import app.core.fetcher as fetcher_module
from app.config import settings
from app.core.extractor import ExtractionError, extract
from app.core.fetcher import FetchError, fetch

# ---------------------------------------------------------------------------
# audit_1 / C1 - SSRF via redirect
# ---------------------------------------------------------------------------

def test_redirect_to_private_address_is_blocked(raw_site, monkeypatch):
    """A public host answering 302 -> 127.0.0.1 must not defeat the SSRF guard.

    Only the attacker's own (notionally public) host passes validation; every
    later hop is validated for real.
    """
    raw_site.redirect_target = f"{raw_site.url}/secret"
    real_validate = fetcher_module.validate_target_url

    def first_hop_public(url: str) -> str:
        return url if url.endswith("/redirect") else real_validate(url)

    monkeypatch.setattr(fetcher_module, "validate_target_url", first_hop_public)
    monkeypatch.setattr(settings, "min_seconds_between_hits_per_host", 0)

    with pytest.raises(FetchError) as exc:
        fetch(f"{raw_site.url}/redirect", respect_robots=False, throttle=False)
    assert exc.value.kind == "blocked_target"


def test_redirect_chain_is_bounded(site):
    """An endless redirect loop must terminate, not spin forever."""
    with pytest.raises(FetchError) as exc:
        fetch(f"{site.url}/redirect", respect_robots=False, throttle=False)
    assert exc.value.kind == "too_many_redirects"


# ---------------------------------------------------------------------------
# audit_1 / H1 - ReDoS through user-supplied regex
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "pattern,payload",
    [
        (r"(a+)+$", "a" * 40),
        (r"(x+x+)+y", "x" * 200),
        (r"(a|a)*$", "a" * 45),
        (r"([a-z]+)+#", "a" * 80),
    ],
)
def test_catastrophic_regex_is_time_bounded(pattern, payload):
    """Under stdlib `re` these run effectively forever and pin a worker thread."""
    body = f"<html><body>{payload}</body></html>"
    started = time.perf_counter()
    try:
        extract(body, "regex", pattern)
    except ExtractionError:
        pass
    elapsed = time.perf_counter() - started
    assert elapsed < 5.0, f"{pattern!r} took {elapsed:.1f}s - ReDoS guard is not holding"


def test_catastrophic_ignore_pattern_is_time_bounded():
    body = "<html><body>" + "a" * 60 + "</body></html>"
    started = time.perf_counter()
    extract(body, "text", ignore_patterns=[r"(a+)+$", r"(a|a)*$"])
    assert time.perf_counter() - started < 8.0


def test_overlong_pattern_is_rejected():
    with pytest.raises(ExtractionError) as exc:
        extract("<p>x</p>", "regex", "a" * 5000)
    assert exc.value.kind == "pattern_too_long"


# ---------------------------------------------------------------------------
# audit_1 / H3, H4 - signup abuse and password guessing
# ---------------------------------------------------------------------------

def test_signup_is_throttled_per_ip(client):
    created = 0
    for i in range(settings.signup_limit_per_hour_per_ip + 5):
        r = client.post(
            "/v1/auth/signup",
            json={"email": f"burner{i}@throwaway.example", "password": "correct-horse-42"},
        )
        if r.status_code == 201:
            created += 1
    assert created == settings.signup_limit_per_hour_per_ip


def test_login_lockout_is_per_account_not_per_ip(client, account):
    codes = []
    for i in range(settings.login_max_failures + 5):
        # Rotate the source address the way a distributed guesser would.
        codes.append(
            client.post(
                "/v1/auth/login",
                json={"email": "dev@example.com", "password": f"guess{i}"},
                headers={"X-Forwarded-For": f"203.0.113.{i}"},
            ).status_code
        )
    assert codes.count(401) == settings.login_max_failures
    assert 429 in codes


def test_successful_login_clears_the_failure_counter(client, account):
    for _ in range(3):
        client.post("/v1/auth/login", json={"email": "dev@example.com", "password": "nope"})
    assert (
        client.post(
            "/v1/auth/login", json={"email": "dev@example.com", "password": "correct-horse-42"}
        ).status_code
        == 200
    )
    # The counter reset, so a fresh run of failures is not immediately locked.
    assert (
        client.post("/v1/auth/login", json={"email": "dev@example.com", "password": "nope"}).status_code
        == 401
    )


def test_signup_message_does_not_name_the_account_as_existing(client, account):
    """Narrows the enumeration oracle. It does NOT close it: the status code
    still differs (400 vs 201). Full closure needs an email-verification flow
    returning an identical response either way - see docs/SECURITY.md."""
    body = client.post(
        "/v1/auth/signup", json={"email": "dev@example.com", "password": "correct-horse-42"}
    ).json()
    message = body["error"]["message"].lower()
    for leak in ("already exists", "already registered", "taken", "in use"):
        assert leak not in message


# ---------------------------------------------------------------------------
# audit_1 / M - unauthenticated business intelligence
# ---------------------------------------------------------------------------

def test_metrics_requires_authentication(client, account):
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers=account["headers"]).status_code == 200


def test_liveness_probe_stays_open(client):
    """Load balancers must still be able to poll without credentials."""
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200


# ---------------------------------------------------------------------------
# audit_1 / H5 - concurrent workers double-claiming monitors
# ---------------------------------------------------------------------------

def test_concurrent_workers_never_claim_the_same_monitor(tmp_path):
    """Measured before the fix: four workers each claimed all forty due
    monitors, causing four fetches and four webhook sends per check."""
    import threading
    from datetime import datetime, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    from app.core.engine import claim_due_monitors
    from app.models import Monitor, User
    from app.security import hash_password

    db_path = tmp_path / "claims.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    Base.metadata.create_all(bind=engine)

    setup = Session()
    user = User(email="race@example.com", password_hash=hash_password("correct-horse-42"), plan="business")
    setup.add(user)
    setup.commit()
    for i in range(40):
        setup.add(
            Monitor(
                user_id=user.id, name=f"m{i}", url="https://example.com/",
                extract_mode="text", interval_seconds=60,
                notify_webhook_url="https://example.com/hook",
                next_check_at=datetime.now(timezone.utc),
            )
        )
    setup.commit()
    setup.close()

    claimed: list[int] = []
    lock = threading.Lock()

    def worker():
        session = Session()
        try:
            got = claim_due_monitors(session, 50)
            with lock:
                claimed.extend(m.id for m in got)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == len(set(claimed)), "a monitor was claimed by more than one worker"
    assert len(claimed) == 40
    engine.dispose()


# ---------------------------------------------------------------------------
# audit_2 - findings from the second round
# ---------------------------------------------------------------------------

def test_forwarded_header_cannot_buy_extra_signups(client, monkeypatch):
    """Round 1 keyed the throttle on X-Forwarded-For unconditionally, which the
    client controls: measured 20/20 accounts against a limit of 5."""
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    created = 0
    for i in range(20):
        r = client.post(
            "/v1/auth/signup",
            json={"email": f"spoof{i}@throwaway.example", "password": "correct-horse-42"},
            headers={"X-Forwarded-For": f"198.51.100.{i}"},
        )
        if r.status_code == 201:
            created += 1
    assert created == settings.signup_limit_per_hour_per_ip


def test_lockout_does_not_let_a_stranger_deny_a_user_their_account(client, account, monkeypatch):
    """Brute-force protection must not become a denial-of-service tool.

    Round 1 locked purely on the account, so 8 failures from anywhere denied the
    owner access - anyone who knew an email could lock it at will. The lock is
    now primarily per (account, source); the account-wide tier sits far higher.

    Proxy trust is enabled here only so the test can present distinct client
    addresses; TestClient otherwise reports a single source for every request.
    """
    monkeypatch.setattr(settings, "trust_proxy_headers", True)

    for i in range(settings.login_max_failures + 4):
        client.post(
            "/v1/auth/login",
            json={"email": "dev@example.com", "password": f"bad{i}"},
            headers={"X-Forwarded-For": f"203.0.113.{i}"},
        )

    # The real owner, from their own address, can still get in.
    assert (
        client.post(
            "/v1/auth/login",
            json={"email": "dev@example.com", "password": "correct-horse-42"},
            headers={"X-Forwarded-For": "198.51.100.7"},
        ).status_code
        == 200
    )


def test_repeated_failures_from_one_source_still_lock_out(client, account, monkeypatch):
    """The relaxed lockout must not have disarmed brute-force protection."""
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    codes = [
        client.post(
            "/v1/auth/login",
            json={"email": "dev@example.com", "password": f"bad{i}"},
            headers={"X-Forwarded-For": "203.0.113.9"},
        ).status_code
        for i in range(settings.login_max_failures + 4)
    ]
    assert codes.count(401) == settings.login_max_failures
    assert 429 in codes


def test_account_wide_tier_still_stops_a_distributed_attack(client, account, monkeypatch):
    """Many addresses, one account: the loose account-wide tier must eventually trip."""
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    codes = []
    for i in range(settings.login_max_failures_global + 5):
        codes.append(
            client.post(
                "/v1/auth/login",
                json={"email": "dev@example.com", "password": f"bad{i}"},
                headers={"X-Forwarded-For": f"203.0.{i // 250}.{i % 250}"},
            ).status_code
        )
    assert 429 in codes, "a distributed guesser was never stopped"
    assert codes.count(401) <= settings.login_max_failures_global


def test_fetch_has_a_total_deadline_across_redirect_hops(monkeypatch):
    """The round-1 redirect fix throttles every hop, so a redirect chain
    multiplied wall time: measured 15s at a 3s gap, ~100s at the production 20s."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Chain(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/next")
            self.end_headers()

    srv = HTTPServer(("127.0.0.1", 0), _Chain)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "allow_private_network_targets", True)
    monkeypatch.setattr(settings, "min_seconds_between_hits_per_host", 5)
    monkeypatch.setattr(settings, "fetch_total_deadline_seconds", 3.0)
    try:
        started = time.perf_counter()
        with pytest.raises(FetchError) as exc:
            fetch(f"http://127.0.0.1:{srv.server_address[1]}/", respect_robots=False)
        elapsed = time.perf_counter() - started
        assert exc.value.kind in {"deadline_exceeded", "too_many_redirects"}
        assert elapsed < 10.0, f"one check occupied a worker for {elapsed:.1f}s"
    finally:
        srv.shutdown()
        srv.server_close()


def test_account_deletion_removes_everything(client, account, db):
    """terms.md promises deletion; verify it actually cascades."""
    from app.models import ApiKey, ChangeEvent, Check, Monitor, User

    created = client.post(
        "/v1/monitors",
        json={
            "name": "to delete", "url": "https://example.com/p", "extract_mode": "text",
            "interval_seconds": 3600, "notify_webhook_url": "https://example.com/hook",
        },
        headers=account["headers"],
    ).json()
    monitor_id = created["id"]

    monitor = db.get(Monitor, monitor_id)
    db.add(Check(monitor_id=monitor_id, status="unchanged"))
    db.add(ChangeEvent(monitor_id=monitor_id, old_hash="a", new_hash="b", diff={}))
    db.commit()
    user_id = monitor.user_id

    # Wrong confirmation must not delete anything.
    assert client.request(
        "DELETE", "/v1/auth/me", params={"confirm": "wrong@example.com"},
        headers=account["headers"],
    ).status_code == 400
    assert db.get(User, user_id) is not None

    assert client.request(
        "DELETE", "/v1/auth/me", params={"confirm": "dev@example.com"},
        headers=account["headers"],
    ).status_code == 200

    db.expire_all()
    assert db.get(User, user_id) is None
    assert db.query(Monitor).filter(Monitor.id == monitor_id).count() == 0
    assert db.query(ApiKey).filter(ApiKey.user_id == user_id).count() == 0
    assert db.query(Check).filter(Check.monitor_id == monitor_id).count() == 0
    assert db.query(ChangeEvent).filter(ChangeEvent.monitor_id == monitor_id).count() == 0


def test_account_export_returns_the_users_data(client, account):
    client.post(
        "/v1/monitors",
        json={
            "name": "exported", "url": "https://example.com/p", "extract_mode": "text",
            "interval_seconds": 3600, "notify_webhook_url": "https://example.com/hook",
        },
        headers=account["headers"],
    )
    body = client.get("/v1/auth/me/export", headers=account["headers"]).json()
    assert body["account"]["email"] == "dev@example.com"
    assert [m["name"] for m in body["monitors"]] == ["exported"]


def test_retention_actually_deletes_history_past_the_plan_window(client, account, db):
    """history_days was only used to filter queries - nothing deleted rows, so
    storage grew without bound and the cost model was wrong."""
    from datetime import datetime, timedelta, timezone

    from app.core.retention import storage_stats, sweep
    from app.models import ChangeEvent, Check, Monitor

    created = client.post(
        "/v1/monitors",
        json={
            "name": "retained", "url": "https://example.com/p", "extract_mode": "text",
            "interval_seconds": 3600, "notify_webhook_url": "https://example.com/hook",
        },
        headers=account["headers"],
    ).json()
    monitor_id = created["id"]

    now = datetime.now(timezone.utc)
    old = now - timedelta(days=40)          # free plan keeps 7 days
    for _ in range(5):
        db.add(Check(monitor_id=monitor_id, status="unchanged", started_at=old))
        db.add(ChangeEvent(monitor_id=monitor_id, old_hash="a", new_hash="b", diff={}, created_at=old))
    for _ in range(3):
        db.add(Check(monitor_id=monitor_id, status="unchanged", started_at=now))
    db.commit()

    assert storage_stats(db)["checks"] == 8
    sweep(db)
    db.expire_all()

    stats = storage_stats(db)
    assert stats["checks"] == 3, "stale checks were not pruned"
    assert stats["change_events"] == 0, "stale change events were not pruned"
    # The monitor itself must survive a retention sweep.
    assert db.get(Monitor, monitor_id) is not None
