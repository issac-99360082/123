"""Billing: graceful degradation without Stripe, and webhook-driven plan sync."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.api import billing as billing_module
from app.config import settings
from app.models import Plan, StripeEvent, User


def test_plans_are_public(client):
    plans = client.get("/v1/billing/plans").json()
    assert {p["key"] for p in plans} == {"free", "pro", "business"}
    assert next(p for p in plans if p["key"] == "pro")["price_usd_month"] == 19


def test_plans_marks_the_callers_current_plan(client, account):
    plans = client.get("/v1/billing/plans", headers=account["headers"]).json()
    assert next(p for p in plans if p["current"])["key"] == "free"


def test_checkout_is_unavailable_without_stripe_credentials(client, account):
    response = client.post("/v1/billing/checkout?plan=pro", headers=account["headers"])
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "service_unavailable"
    assert "STRIPE_SECRET_KEY" in response.json()["error"]["message"]


def test_webhook_is_rejected_without_stripe_credentials(client):
    response = client.post("/v1/billing/webhook", content=b"{}")
    assert response.status_code == 503


def test_subscription_endpoint_describes_the_free_plan(client, account):
    body = client.get("/v1/billing/subscription", headers=account["headers"]).json()
    assert body["plan"] == "free"
    assert body["billing_enabled"] is False
    assert body["limits"]["max_monitors"] == 3


@pytest.fixture
def stripe_configured(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_fake")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_fake")
    monkeypatch.setattr(settings, "stripe_price_pro", "price_pro_123")
    monkeypatch.setattr(settings, "stripe_price_business", "price_biz_456")


def subscription_object(user_id: int, price_id: str, status: str = "active") -> dict:
    return {
        "id": "sub_test_1",
        "status": status,
        "customer": "cus_test_1",
        "metadata": {"watchpost_user_id": str(user_id)},
        "current_period_end": int(datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()),
        "items": {"data": [{"price": {"id": price_id}}]},
    }


def fake_event(event_id: str, event_type: str, obj: dict) -> dict:
    return {"id": event_id, "type": event_type, "data": {"object": obj}}


def post_webhook(client, monkeypatch, event: dict):
    monkeypatch.setattr(
        billing_module.stripe.Webhook, "construct_event", staticmethod(lambda *a, **k: event)
    )
    return client.post(
        "/v1/billing/webhook", content=b"{}", headers={"Stripe-Signature": "t=1,v1=x"}
    )


def test_active_subscription_upgrades_the_plan(client, db, account, stripe_configured, monkeypatch):
    user = db.query(User).filter(User.email == "dev@example.com").one()
    event = fake_event(
        "evt_1", "customer.subscription.updated", subscription_object(user.id, "price_pro_123")
    )
    assert post_webhook(client, monkeypatch, event).status_code == 200

    db.expire_all()
    user = db.query(User).filter(User.email == "dev@example.com").one()
    assert user.plan == Plan.pro.value
    assert user.subscription_status == "active"
    assert user.stripe_subscription_id == "sub_test_1"


def test_business_price_maps_to_the_business_plan(client, db, account, stripe_configured, monkeypatch):
    user = db.query(User).filter(User.email == "dev@example.com").one()
    event = fake_event(
        "evt_2", "customer.subscription.updated", subscription_object(user.id, "price_biz_456")
    )
    post_webhook(client, monkeypatch, event)

    db.expire_all()
    assert db.query(User).filter(User.email == "dev@example.com").one().plan == Plan.business.value


def test_cancellation_drops_the_user_back_to_free(client, db, account, stripe_configured, monkeypatch):
    user = db.query(User).filter(User.email == "dev@example.com").one()
    user.plan = Plan.pro.value
    user.stripe_subscription_id = "sub_test_1"
    db.commit()

    event = fake_event(
        "evt_3", "customer.subscription.deleted", subscription_object(user.id, "price_pro_123", "canceled")
    )
    assert post_webhook(client, monkeypatch, event).status_code == 200

    db.expire_all()
    user = db.query(User).filter(User.email == "dev@example.com").one()
    assert user.plan == Plan.free.value
    assert user.subscription_status == "canceled"
    assert user.stripe_subscription_id is None


def test_unpaid_subscription_does_not_keep_a_paid_plan(client, db, account, stripe_configured, monkeypatch):
    user = db.query(User).filter(User.email == "dev@example.com").one()
    event = fake_event(
        "evt_4", "customer.subscription.updated", subscription_object(user.id, "price_pro_123", "unpaid")
    )
    post_webhook(client, monkeypatch, event)

    db.expire_all()
    assert db.query(User).filter(User.email == "dev@example.com").one().plan == Plan.free.value


def test_webhook_replay_is_idempotent(client, db, account, stripe_configured, monkeypatch):
    user = db.query(User).filter(User.email == "dev@example.com").one()
    event = fake_event(
        "evt_5", "customer.subscription.updated", subscription_object(user.id, "price_pro_123")
    )
    assert post_webhook(client, monkeypatch, event).json()["message"] == "ok"
    assert post_webhook(client, monkeypatch, event).json()["message"] == "duplicate event ignored"
    assert db.query(StripeEvent).filter(StripeEvent.id == "evt_5").count() == 1


def test_bad_signature_is_rejected(client, db, account, stripe_configured, monkeypatch):
    def raise_sig_error(*args, **kwargs):
        raise billing_module.stripe.SignatureVerificationError("bad sig", "sig_header")

    monkeypatch.setattr(
        billing_module.stripe.Webhook, "construct_event", staticmethod(raise_sig_error)
    )
    response = client.post(
        "/v1/billing/webhook", content=b"{}", headers={"Stripe-Signature": "t=1,v1=forged"}
    )
    assert response.status_code == 422
    assert db.query(StripeEvent).count() == 0, "an unverified event must never be recorded"


def test_upgrade_actually_raises_the_enforced_limits(client, db, account, stripe_configured, monkeypatch):
    payload = {
        "name": "fast",
        "url": "https://example.com/pricing",
        "extract_mode": "css",
        "extract_value": ".price",
        "interval_seconds": 300,
        "notify_webhook_url": "https://example.com/hooks/wp",
    }
    assert client.post("/v1/monitors", json=payload, headers=account["headers"]).status_code == 402

    user = db.query(User).filter(User.email == "dev@example.com").one()
    post_webhook(
        client, monkeypatch,
        fake_event("evt_6", "customer.subscription.updated", subscription_object(user.id, "price_pro_123")),
    )

    assert client.post("/v1/monitors", json=payload, headers=account["headers"]).status_code == 201
