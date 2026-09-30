"""Test fixtures.

Every test runs against a fresh in-memory SQLite database and a TestClient
whose lifespan does not start the scheduler - tests drive `tick()` explicitly
so timing is deterministic.
"""

from __future__ import annotations

import os

os.environ.update(
    {
        "DATABASE_URL": "sqlite:///:memory:",
        "SCHEDULER_ENABLED": "false",
        "RESPECT_ROBOTS_TXT": "false",
        "MIN_SECONDS_BETWEEN_HITS_PER_HOST": "0",
        "LOG_LEVEL": "WARNING",
        "ENVIRONMENT": "development",
        "RATE_LIMIT_PER_MINUTE": "100000",
        "RATE_LIMIT_BURST": "100000",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings, settings  # noqa: E402

get_settings.cache_clear()

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import User  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    """Auth throttles are process-global, so reset them between tests exactly
    as the database is reset - otherwise test order changes test outcomes."""
    from app import deps

    deps._signup_limiter._events.clear()
    deps._login_failures._events.clear()
    deps._limiter._buckets.clear()
    yield


@pytest.fixture(autouse=True)
def fresh_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


@pytest.fixture
def account(client) -> dict:
    """A signed-up user with an API key and ready-to-use auth headers."""
    response = client.post(
        "/v1/auth/signup", json={"email": "dev@example.com", "password": "correct-horse-42"}
    )
    assert response.status_code == 201, response.text
    key = response.json()["key"]
    return {"key": key, "headers": {"Authorization": f"Bearer {key}"}}


@pytest.fixture
def upgrade_to_business(db):
    """Grant a plan directly, the way a Stripe webhook would."""

    def _upgrade(email: str = "dev@example.com", plan: str = "business"):
        user = db.query(User).filter(User.email == email).one()
        user.plan = plan
        db.commit()
        return user

    return _upgrade


# ---------------------------------------------------------------------------
# A single local HTTP server covering every fault shape the tests need.
#
# Previously six near-identical BaseHTTPRequestHandler subclasses were spread
# across three test files. Per-test state (the price shown, where redirects
# point, POSTs received) lives on the instance, so a test mutates `site.price`
# instead of declaring another handler class.
# ---------------------------------------------------------------------------

import gzip  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

INTERNAL_SECRET = "INTERNAL-ONLY-CONTENT"
GZIP_BOMB = gzip.compress(b"A" * (50 * 1024 * 1024))
REDESIGNED_PAGE = b'<html><body><span data-testid="cost">$19.00</span></body></html>'


class FaultSite:
    """Local server whose responses each test selects by path."""

    def __init__(self):
        self.price = "$19.00"
        self.redirect_target: str | None = None  # None -> redirect to itself (loop)
        self.received: list[dict] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="text/html; charset=utf-8", extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                path, port = self.path, self.server.server_address[1]
                try:
                    if path.startswith("/slow"):
                        # Every chunk arrives inside the socket timeout, so only a
                        # total deadline can stop this.
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html")
                        self.send_header("Transfer-Encoding", "chunked")
                        self.end_headers()
                        for _ in range(120):
                            self.wfile.write(b"c\r\n<div>x</div>\r\n")
                            self.wfile.flush()
                            time.sleep(0.4)
                        self.wfile.write(b"0\r\n\r\n")
                    elif path.startswith("/bomb"):
                        self._send(200, GZIP_BOMB, extra={"Content-Encoding": "gzip"})
                    elif path.startswith("/redirect"):
                        target = site.redirect_target or f"http://127.0.0.1:{port}/redirect"
                        self.send_response(302)
                        self.send_header("Location", target)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    elif path.startswith("/secret"):
                        self._send(200, f"<html><body>{INTERNAL_SECRET}</body></html>".encode())
                    elif path.startswith("/empty"):
                        self._send(200, b"")
                    elif path.startswith("/whitespace"):
                        self._send(200, b"   \n\n\t  ")
                    elif path.startswith("/v2"):
                        self._send(200, REDESIGNED_PAGE)
                    elif path.startswith("/500"):
                        self._send(500, b"err")
                    elif path.startswith("/robots.txt") or path.startswith("/404"):
                        self._send(404, b"not found")
                    else:
                        self._send(
                            200,
                            f'<html><body><h1>Widget</h1>'
                            f'<div class="price">{site.price}</div></body></html>'.encode(),
                        )
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                site.received.append(
                    {"headers": {k.lower(): v for k, v in self.headers.items()}, "raw": raw}
                )
                self._send(200)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        self.port = self._srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def json_payloads(self) -> list[dict]:
        import json

        return [json.loads(r["raw"].decode()) for r in self.received]

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture
def site(monkeypatch):
    """A local fault site, with the SSRF guard and host throttle relaxed for it."""
    monkeypatch.setattr(settings, "allow_private_network_targets", True)
    monkeypatch.setattr(settings, "min_seconds_between_hits_per_host", 0)
    server = FaultSite()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def raw_site():
    """A fault site WITHOUT the SSRF guard relaxed - for tests that must see it fire."""
    server = FaultSite()
    try:
        yield server
    finally:
        server.close()
