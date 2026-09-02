"""Regression tests for findings from the production-readiness audits.

Each test here corresponds to a confirmed, exploited vulnerability. They exist
so a future refactor cannot quietly reopen one.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import app.core.fetcher as fetcher_module
from app.config import settings
from app.core.extractor import ExtractionError, extract
from app.core.fetcher import FetchError, fetch

# ---------------------------------------------------------------------------
# audit_1 / C1 - SSRF via redirect
# ---------------------------------------------------------------------------

INTERNAL_SECRET = "INTERNAL-ONLY-CONTENT"


class _Internal(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = f"<html><body>{INTERNAL_SECRET}</body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def internal_server():
    srv = HTTPServer(("127.0.0.1", 0), _Internal)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def redirector(internal_server):
    target = f"http://127.0.0.1:{internal_server}/secret"

    class _Redirect(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()

    srv = HTTPServer(("127.0.0.1", 0), _Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def test_redirect_to_private_address_is_blocked(redirector, monkeypatch):
    """A public host answering 302 -> 127.0.0.1 must not defeat the SSRF guard.

    Only the attacker's own (notionally public) host is allowed through
    validation; every later hop is validated for real.
    """
    real_validate = fetcher_module.validate_target_url

    def first_hop_public(url: str) -> str:
        if f":{redirector}" in url:
            return url
        return real_validate(url)

    monkeypatch.setattr(fetcher_module, "validate_target_url", first_hop_public)

    with pytest.raises(FetchError) as exc:
        fetch(f"http://127.0.0.1:{redirector}/start", respect_robots=False, throttle=False)
    assert exc.value.kind == "blocked_target"


def test_redirect_chain_is_bounded(monkeypatch):
    """An endless redirect loop must terminate, not spin forever."""

    class _Loop(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/next")
            self.end_headers()

    srv = HTTPServer(("127.0.0.1", 0), _Loop)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "allow_private_network_targets", True)
    try:
        with pytest.raises(FetchError) as exc:
            fetch(f"http://127.0.0.1:{srv.server_address[1]}/", respect_robots=False, throttle=False)
        assert exc.value.kind == "too_many_redirects"
    finally:
        srv.shutdown()
        srv.server_close()


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
