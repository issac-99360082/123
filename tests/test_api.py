"""HTTP surface: auth, monitor CRUD, ownership isolation, quotas, error shape."""

from __future__ import annotations

import pytest


def test_healthz_needs_no_auth(client):
    response = client.get("/healthz")
    assert response.status_code == 200 and response.json()["status"] == "ok"


def test_readyz_reports_dependency_state(client):
    body = client.get("/readyz").json()
    assert body["checks"]["database"] == "ok"


def test_landing_page_renders(client):
    response = client.get("/")
    assert response.status_code == 200 and "Watchpost" in response.text


def test_signup_returns_a_key_once(client):
    response = client.post(
        "/v1/auth/signup", json={"email": "New@Example.com", "password": "correct-horse-42"}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["key"].startswith("wp_live_")
    assert body["prefix"] in body["key"]

    # The key is never retrievable afterwards.
    listed = client.get("/v1/auth/keys", headers={"Authorization": f"Bearer {body['key']}"}).json()
    assert "key" not in listed[0]


def test_signup_rejects_duplicate_email_case_insensitively(client, account):
    response = client.post(
        "/v1/auth/signup", json={"email": "DEV@example.com", "password": "correct-horse-42"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "bad_request"


@pytest.mark.parametrize("password", ["short", "1234567890", "abcdefghijkl"])
def test_weak_passwords_are_rejected(client, password):
    response = client.post("/v1/auth/signup", json={"email": "w@example.com", "password": password})
    assert response.status_code == 422


def test_login_is_generic_about_why_it_failed(client, account):
    wrong_password = client.post(
        "/v1/auth/login", json={"email": "dev@example.com", "password": "wrong-horse-99"}
    )
    unknown_user = client.post(
        "/v1/auth/login", json={"email": "ghost@example.com", "password": "correct-horse-42"}
    )
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json(), "response must not reveal account existence"


def test_missing_and_bad_keys_are_rejected(client):
    assert client.get("/v1/auth/me").status_code == 401
    assert client.get("/v1/auth/me", headers={"Authorization": "Bearer wp_live_nope"}).status_code == 401


def test_x_api_key_header_also_works(client, account):
    assert client.get("/v1/auth/me", headers={"X-API-Key": account["key"]}).status_code == 200


def test_revoked_key_stops_working(client, account):
    key_id = client.get("/v1/auth/keys", headers=account["headers"]).json()[0]["id"]
    assert client.delete(f"/v1/auth/keys/{key_id}", headers=account["headers"]).status_code == 200
    assert client.get("/v1/auth/me", headers=account["headers"]).status_code == 401


def monitor_payload(**overrides) -> dict:
    payload = {
        "name": "Competitor pricing",
        "url": "https://example.com/pricing",
        "extract_mode": "css",
        "extract_value": ".price",
        "interval_seconds": 3600,
        "notify_webhook_url": "https://hooks.example.com/wp",
    }
    payload.update(overrides)
    return payload


def test_create_monitor_returns_the_webhook_secret_once(client, account):
    response = client.post("/v1/monitors", json=monitor_payload(), headers=account["headers"])
    assert response.status_code == 201
    body = response.json()
    assert body["webhook_secret"].startswith("whsec_")

    fetched = client.get(f"/v1/monitors/{body['id']}", headers=account["headers"]).json()
    assert "webhook_secret" not in fetched, "the signing secret must not be re-served"


def test_monitor_requires_a_notification_channel(client, account):
    response = client.post(
        "/v1/monitors",
        json=monitor_payload(notify_webhook_url=None),
        headers=account["headers"],
    )
    assert response.status_code == 422
    assert "notify_webhook_url" in response.json()["error"]["message"]


def test_selector_modes_require_a_selector(client, account):
    response = client.post(
        "/v1/monitors",
        json=monitor_payload(extract_value=None),
        headers=account["headers"],
    )
    assert response.status_code == 422


def test_ssrf_target_is_rejected_at_write_time(client, account):
    response = client.post(
        "/v1/monitors",
        json=monitor_payload(url="http://169.254.169.254/latest/meta-data/"),
        headers=account["headers"],
    )
    assert response.status_code == 422
    assert "Invalid url" in response.json()["error"]["message"]


def test_ssrf_webhook_target_is_rejected(client, account):
    response = client.post(
        "/v1/monitors",
        json=monitor_payload(notify_webhook_url="http://127.0.0.1:9000/hook"),
        headers=account["headers"],
    )
    assert response.status_code == 422


def test_invalid_ignore_pattern_is_rejected(client, account):
    response = client.post(
        "/v1/monitors", json=monitor_payload(ignore_patterns=["(unclosed"]), headers=account["headers"]
    )
    assert response.status_code == 422


def test_free_plan_interval_floor_is_enforced(client, account):
    response = client.post(
        "/v1/monitors", json=monitor_payload(interval_seconds=60), headers=account["headers"]
    )
    assert response.status_code == 402
    assert response.json()["error"]["type"] == "quota_exceeded"
    assert response.json()["error"]["detail"]["min_interval_seconds"] == 3600


def test_free_plan_monitor_cap_is_enforced(client, account):
    for i in range(3):
        created = client.post(
            "/v1/monitors", json=monitor_payload(name=f"m{i}"), headers=account["headers"]
        )
        assert created.status_code == 201

    blocked = client.post("/v1/monitors", json=monitor_payload(name="m4"), headers=account["headers"])
    assert blocked.status_code == 402
    assert blocked.json()["error"]["detail"]["max_monitors"] == 3


def test_upgrading_the_plan_lifts_the_limits(client, account, upgrade_to_business):
    assert (
        client.post("/v1/monitors", json=monitor_payload(interval_seconds=60), headers=account["headers"]).status_code
        == 402
    )
    upgrade_to_business()
    assert (
        client.post("/v1/monitors", json=monitor_payload(interval_seconds=60), headers=account["headers"]).status_code
        == 201
    )


def test_monitors_are_isolated_between_accounts(client, account):
    mine = client.post("/v1/monitors", json=monitor_payload(), headers=account["headers"]).json()

    other_key = client.post(
        "/v1/auth/signup", json={"email": "other@example.com", "password": "correct-horse-42"}
    ).json()["key"]
    other_headers = {"Authorization": f"Bearer {other_key}"}

    assert client.get(f"/v1/monitors/{mine['id']}", headers=other_headers).status_code == 404
    assert client.patch(f"/v1/monitors/{mine['id']}", json={"name": "hijacked"}, headers=other_headers).status_code == 404
    assert client.delete(f"/v1/monitors/{mine['id']}", headers=other_headers).status_code == 404
    assert client.get("/v1/monitors", headers=other_headers).json() == []
    # And the original is untouched.
    assert client.get(f"/v1/monitors/{mine['id']}", headers=account["headers"]).json()["name"] == "Competitor pricing"


def test_changing_the_selector_resets_the_baseline(client, account, db):
    from app.models import Monitor

    created = client.post("/v1/monitors", json=monitor_payload(), headers=account["headers"]).json()
    monitor = db.get(Monitor, created["id"])
    monitor.last_content_hash = "abc123"
    monitor.last_content = "$19.00"
    db.commit()

    client.patch(f"/v1/monitors/{created['id']}", json={"extract_value": ".new-price"}, headers=account["headers"])
    db.expire_all()
    monitor = db.get(Monitor, created["id"])
    assert monitor.last_content_hash is None, "a new selector must not report a false change"


def test_reactivating_a_paused_monitor_clears_the_failure_state(client, account, db):
    from app.models import Monitor

    created = client.post("/v1/monitors", json=monitor_payload(), headers=account["headers"]).json()
    monitor = db.get(Monitor, created["id"])
    monitor.is_active = False
    monitor.consecutive_failures = 20
    monitor.paused_reason = "auto-paused"
    db.commit()

    body = client.patch(
        f"/v1/monitors/{created['id']}", json={"is_active": True}, headers=account["headers"]
    ).json()
    assert body["is_active"] is True
    assert body["consecutive_failures"] == 0
    assert body["paused_reason"] is None


def test_usage_endpoint_reflects_the_plan(client, account):
    client.post("/v1/monitors", json=monitor_payload(), headers=account["headers"])
    usage = client.get("/v1/usage", headers=account["headers"]).json()
    assert usage["plan"] == "free"
    assert usage["monitors_used"] == 1 and usage["monitors_allowed"] == 3
    assert usage["min_interval_seconds"] == 3600


def test_delete_removes_the_monitor(client, account):
    created = client.post("/v1/monitors", json=monitor_payload(), headers=account["headers"]).json()
    assert client.delete(f"/v1/monitors/{created['id']}", headers=account["headers"]).status_code == 200
    assert client.get(f"/v1/monitors/{created['id']}", headers=account["headers"]).status_code == 404


def test_errors_share_one_envelope(client):
    body = client.get("/v1/monitors").json()
    assert set(body) == {"error"}
    assert {"type", "message"} <= set(body["error"])


def test_security_headers_are_present(client):
    headers = client.get("/healthz").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "X-Request-ID" in headers
