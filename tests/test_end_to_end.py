"""End-to-end: a real HTTP server, the real scheduler tick, a real webhook receiver.

Nothing here is mocked except the SSRF guard (the test server is on localhost),
so this exercises fetch -> extract -> diff -> queue -> deliver as one system.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.config import settings
from app.core.scheduler import tick
from app.models import ChangeEvent, Delivery, DeliveryStatus, Monitor, User
from app.security import sign_webhook

# State shared with the handler threads.
PAGE_PRICE = {"value": "$19.00"}
RECEIVED: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence per-request stderr noise
        pass

    def do_GET(self):
        if self.path == "/pricing":
            body = (
                f'<html><body><h1>Widget</h1>'
                f'<div class="price">{PAGE_PRICE["value"]}</div></body></html>'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/robots.txt":
            self.send_response(404)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        RECEIVED.append(
            {
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "raw": raw,
                "json": json.loads(raw.decode()),
            }
        )
        self.send_response(200)
        self.end_headers()


@pytest.fixture
def server():
    PAGE_PRICE["value"] = "$19.00"
    RECEIVED.clear()
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def allow_localhost(monkeypatch):
    """The SSRF guard is doing its job; the test server is deliberately local."""
    monkeypatch.setattr(settings, "allow_private_network_targets", True)


def test_full_lifecycle_signup_to_signed_webhook(client, db, server, allow_localhost):
    # 1. Self-serve signup - no human in the loop.
    signup = client.post(
        "/v1/auth/signup", json={"email": "e2e@example.com", "password": "correct-horse-42"}
    )
    assert signup.status_code == 201
    headers = {"Authorization": f"Bearer {signup.json()['key']}"}

    # 2. Preview the extraction before committing to a monitor.
    preview = client.post(
        "/v1/preview",
        json={"url": f"{server}/pricing", "extract_mode": "css", "extract_value": ".price"},
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["extracted"] == "$19.00"

    # 3. Create the monitor.
    created = client.post(
        "/v1/monitors",
        json={
            "name": "E2E price",
            "url": f"{server}/pricing",
            "extract_mode": "css",
            "extract_value": ".price",
            "interval_seconds": 3600,
            "notify_webhook_url": f"{server}/hook",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    monitor_id = created.json()["id"]
    secret = created.json()["webhook_secret"]

    # 4. First tick records the baseline and must NOT notify.
    result = tick()
    assert result["monitors_checked"] == 1
    assert RECEIVED == [], "a first observation is not a change"

    # 5. Nothing changed upstream -> still silent.
    db.query(Monitor).filter(Monitor.id == monitor_id).update({"next_check_at": _now()})
    db.commit()
    tick()
    assert RECEIVED == []

    # 6. The page changes -> exactly one signed webhook arrives.
    PAGE_PRICE["value"] = "$17.50"
    db.query(Monitor).filter(Monitor.id == monitor_id).update({"next_check_at": _now()})
    db.commit()
    tick()

    assert len(RECEIVED) == 1, "a real change must notify exactly once"
    delivered = RECEIVED[0]
    payload = delivered["json"]
    assert payload["event"] == "monitor.changed"
    assert payload["monitor"]["id"] == monitor_id
    assert payload["change"]["modified"] == [{"before": "$19.00", "after": "$17.50"}]

    # 7. The signature verifies with the secret we were given at creation.
    timestamp = delivered["headers"]["x-watchpost-timestamp"]
    signature = delivered["headers"]["x-watchpost-signature"]
    assert signature == f"t={timestamp},v1={sign_webhook(secret, timestamp, delivered['raw'])}"

    # 8. The change is queryable through the API and the delivery is recorded.
    changes = client.get(f"/v1/monitors/{monitor_id}/changes", headers=headers).json()
    assert len(changes) == 1 and changes[0]["diff"]["lines_modified"] == 1

    checks = client.get(f"/v1/monitors/{monitor_id}/checks", headers=headers).json()
    assert [c["status"] for c in checks] == ["changed", "unchanged", "unchanged"]

    assert db.query(Delivery).one().status == DeliveryStatus.delivered.value

    # 9. Usage was metered for billing.
    usage = client.get("/v1/usage", headers=headers).json()
    assert usage["checks_used"] == 3 and usage["changes_detected"] == 1


def test_manual_run_endpoint_detects_a_change(client, db, server, allow_localhost):
    signup = client.post(
        "/v1/auth/signup", json={"email": "manual@example.com", "password": "correct-horse-42"}
    )
    headers = {"Authorization": f"Bearer {signup.json()['key']}"}
    created = client.post(
        "/v1/monitors",
        json={
            "name": "manual",
            "url": f"{server}/pricing",
            "extract_mode": "css",
            "extract_value": ".price",
            "interval_seconds": 3600,
            "notify_webhook_url": f"{server}/hook",
        },
        headers=headers,
    ).json()

    assert client.post(f"/v1/monitors/{created['id']}/run", headers=headers).json()["status"] == "unchanged"

    PAGE_PRICE["value"] = "$12.00"
    second = client.post(f"/v1/monitors/{created['id']}/run", headers=headers).json()
    assert second["status"] == "changed"
    assert db.query(ChangeEvent).count() == 1


def test_unreachable_target_is_recorded_as_an_error(client, db, server, allow_localhost):
    signup = client.post(
        "/v1/auth/signup", json={"email": "err@example.com", "password": "correct-horse-42"}
    )
    headers = {"Authorization": f"Bearer {signup.json()['key']}"}
    created = client.post(
        "/v1/monitors",
        json={
            "name": "missing page",
            "url": f"{server}/does-not-exist",
            "extract_mode": "text",
            "interval_seconds": 3600,
            "notify_webhook_url": f"{server}/hook",
        },
        headers=headers,
    ).json()

    check = client.post(f"/v1/monitors/{created['id']}/run", headers=headers).json()
    assert check["status"] == "error"
    assert check["error_type"] == "http_error"
    assert check["http_status"] == 404
    assert RECEIVED == [], "errors must not fire change notifications"


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
