"""Stripe billing: checkout, customer portal, and the webhook that syncs state.

Design rule: Stripe is the source of truth. The app never grants a paid plan
because a user came back from a checkout URL - it grants it only when a
signature-verified webhook says the subscription is active. That closes the
"replay the success URL" hole.

Without Stripe credentials the whole module degrades gracefully: /plans still
works, and the paid endpoints return a clear 503 instead of crashing.
"""

from __future__ import annotations

from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import get_current_user, rate_limit
from app.errors import AppError, UpstreamUnavailableError, ValidationFailedError
from app.logging_conf import get_logger
from app.models import Plan, StripeEvent, User
from app.plans import PLANS, get_plan
from app.schemas import CheckoutSessionOut, MessageOut, PlanOut, PortalSessionOut

router = APIRouter(prefix="/v1/billing", tags=["billing"])
log = get_logger(__name__)

# Maps our plan key -> the Stripe Price id that sells it.
PRICE_ENV = {
    Plan.pro.value: "stripe_price_pro",
    Plan.business.value: "stripe_price_business",
}
# Subscription statuses that keep a paid plan switched on.
ACTIVE_STATUSES = {"active", "trialing", "past_due"}


def _client() -> None:
    if not settings.billing_enabled:
        raise UpstreamUnavailableError(
            "Billing is not configured on this deployment. "
            "Set STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET (see docs/SETUP.md)."
        )
    stripe.api_key = settings.stripe_secret_key


def _price_id_for(plan_key: str) -> str:
    attr = PRICE_ENV.get(plan_key)
    if attr is None:
        raise ValidationFailedError(f"Plan {plan_key!r} is not purchasable.")
    price_id = getattr(settings, attr)
    if not price_id:
        raise UpstreamUnavailableError(
            f"No Stripe price configured for plan {plan_key!r} (set {attr.upper()})."
        )
    return price_id


def _plan_key_for_price(price_id: str | None) -> str | None:
    if not price_id:
        return None
    for plan_key, attr in PRICE_ENV.items():
        if getattr(settings, attr) == price_id:
            return plan_key
    return None


@router.get("/plans", response_model=list[PlanOut], dependencies=[Depends(rate_limit)])
def list_plans(request: Request, db: Session = Depends(get_db)) -> list[PlanOut]:
    """Public pricing. Callable without an API key so the landing page can render it."""
    current_key = None
    auth = request.headers.get("authorization") or request.headers.get("x-api-key")
    if auth:
        try:
            current_key = get_current_user(request, db).plan
        except AppError:
            current_key = None

    return [
        PlanOut(**{**spec.__dict__, "current": spec.key == current_key})
        for spec in PLANS.values()
    ]


@router.post("/checkout", response_model=CheckoutSessionOut, dependencies=[Depends(rate_limit)])
def create_checkout_session(
    plan: str = "pro",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CheckoutSessionOut:
    """Return a Stripe Checkout URL. This is the entire purchase flow - no sales call."""
    _client()
    price_id = _price_id_for(plan)

    try:
        if not user.stripe_customer_id:
            customer = stripe.Customer.create(
                email=user.email, metadata={"watchpost_user_id": str(user.id)}
            )
            user.stripe_customer_id = customer.id
            db.commit()

        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=user.stripe_customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{settings.public_base_url}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{settings.public_base_url}/billing/cancelled",
            client_reference_id=str(user.id),
            metadata={"watchpost_user_id": str(user.id), "plan": plan},
            allow_promotion_codes=True,
        )
    except stripe.StripeError as exc:
        log.error("stripe_checkout_failed", user_id=user.id, error=str(exc))
        raise UpstreamUnavailableError(f"Stripe rejected the checkout request: {exc}") from exc

    log.info("checkout_session_created", user_id=user.id, plan=plan)
    return CheckoutSessionOut(checkout_url=session.url)


@router.post("/portal", response_model=PortalSessionOut, dependencies=[Depends(rate_limit)])
def create_portal_session(user: User = Depends(get_current_user)) -> PortalSessionOut:
    """Stripe-hosted portal: the user cancels, upgrades and gets invoices without us."""
    _client()
    if not user.stripe_customer_id:
        raise AppError("No billing account yet. Start a subscription first.")
    try:
        session = stripe.billing_portal.Session.create(
            customer=user.stripe_customer_id,
            return_url=f"{settings.public_base_url}/app",
        )
    except stripe.StripeError as exc:
        raise UpstreamUnavailableError(f"Stripe rejected the portal request: {exc}") from exc
    return PortalSessionOut(portal_url=session.url)


def _apply_subscription(db: Session, user: User, subscription: dict) -> None:
    """Mirror one Stripe subscription onto the user row."""
    status = subscription.get("status")
    items = (subscription.get("items") or {}).get("data") or []
    price_id = items[0].get("price", {}).get("id") if items else None
    plan_key = _plan_key_for_price(price_id)

    user.stripe_subscription_id = subscription.get("id")
    user.subscription_status = status

    period_end = subscription.get("current_period_end") or (
        items[0].get("current_period_end") if items else None
    )
    user.current_period_end = (
        datetime.fromtimestamp(period_end, tz=timezone.utc) if period_end else None
    )

    if status in ACTIVE_STATUSES and plan_key:
        user.plan = plan_key
    else:
        user.plan = Plan.free.value

    db.commit()
    log.info(
        "subscription_synced",
        user_id=user.id, plan=user.plan, status=status, subscription_id=user.stripe_subscription_id,
    )


def _as_user_id(value: object) -> int | None:
    """Stripe metadata is free-form text; never let it raise into a 500."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        log.warning("stripe_metadata_user_id_invalid", value=str(value)[:64])
        return None


def _find_user(db: Session, obj: dict) -> User | None:
    """Resolve the account an event belongs to, most reliable identifier first."""
    metadata = obj.get("metadata") or {}
    for candidate in (metadata.get("watchpost_user_id"), obj.get("client_reference_id")):
        user_id = _as_user_id(candidate)
        if user_id is not None and (user := db.get(User, user_id)):
            return user
    if customer_id := obj.get("customer"):
        return db.scalar(select(User).where(User.stripe_customer_id == customer_id))
    return None


def _is_stale(user: User, event_created: int | None) -> bool:
    """True when this event is older than the newest one already applied.

    Stripe explicitly does not guarantee webhook ordering. Measured without this
    guard: a cancellation was applied, then a stale `subscription.updated` that
    Stripe delivered late re-upgraded the cancelled account to Pro, which then
    kept paid features indefinitely with no active subscription.
    """
    if event_created is None or user.last_stripe_event_at is None:
        return False
    return event_created < user.last_stripe_event_at


def _mark_applied(db: Session, user: User, event_created: int | None) -> None:
    if event_created is not None and (
        user.last_stripe_event_at is None or event_created > user.last_stripe_event_at
    ):
        user.last_stripe_event_at = event_created
        db.commit()


SUBSCRIPTION_EVENTS = {
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
}
HANDLED_EVENTS = SUBSCRIPTION_EVENTS | {
    "checkout.session.completed",
    "invoice.payment_failed",
}


def _process_event(
    db: Session, event_type: str, obj: dict, event_created: int | None = None
) -> None:
    """Apply one Stripe event. Raises on transient failure so the caller can 503
    and let Stripe redeliver.

    Resolving the account and rejecting out-of-order events happens ONCE, here,
    rather than inside each branch. That is deliberate: the staleness check is
    what stops a late `subscription.updated` from re-upgrading a cancelled
    account, and a branch that forgets it silently gives away paid features. Do
    the check in the dispatcher and a new branch cannot forget it.
    """
    if event_type not in HANDLED_EVENTS:
        log.debug("stripe_event_ignored", type=event_type)
        return

    user = _find_user(db, obj)
    if user is None:
        log.info("stripe_event_no_matching_user", type=event_type)
        return
    if _is_stale(user, event_created):
        log.info("stripe_event_stale_ignored", type=event_type, user_id=user.id)
        return

    if event_type == "checkout.session.completed":
        if not obj.get("subscription"):
            return
        if not user.stripe_customer_id and obj.get("customer"):
            user.stripe_customer_id = obj["customer"]
        subscription = stripe.Subscription.retrieve(obj["subscription"])
        _apply_subscription(db, user, dict(subscription))

    elif event_type == "customer.subscription.deleted":
        user.plan = Plan.free.value
        user.subscription_status = "canceled"
        user.stripe_subscription_id = None
        db.commit()
        log.info("subscription_cancelled", user_id=user.id)

    elif event_type in SUBSCRIPTION_EVENTS:
        _apply_subscription(db, user, dict(obj))

    else:  # invoice.payment_failed
        user.subscription_status = "past_due"
        db.commit()
        log.warning("invoice_payment_failed", user_id=user.id)

    _mark_applied(db, user, event_created)


@router.post("/webhook", response_model=MessageOut)
async def stripe_webhook(
    request: Request,
    db: Session = Depends(get_db),
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
) -> MessageOut:
    """Signature-verified, idempotent subscription sync."""
    if not settings.billing_enabled:
        raise UpstreamUnavailableError("Billing is not configured on this deployment.")

    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, settings.stripe_webhook_secret
        )
    except ValueError as exc:
        raise ValidationFailedError("Malformed Stripe payload.") from exc
    except stripe.SignatureVerificationError as exc:
        log.warning("stripe_webhook_bad_signature")
        raise ValidationFailedError("Stripe signature verification failed.") from exc

    event_id = event["id"]
    if db.get(StripeEvent, event_id):
        return MessageOut(message="duplicate event ignored")

    obj = event["data"]["object"]
    event_type = event["type"]
    stripe.api_key = settings.stripe_secret_key

    # Marked processed ONLY after the work succeeds. Recording it first loses
    # money: a failed Stripe call leaves the customer paid but on Free, and the
    # redelivery is then discarded as a duplicate.
    try:
        _process_event(db, event_type, obj, event.get("created"))
    except stripe.StripeError as exc:
        db.rollback()
        log.error(
            "stripe_event_transient_failure",
            event_id=event_id, type=event_type, error=str(exc),
        )
        # 503 tells Stripe to retry; the event is deliberately NOT recorded.
        raise UpstreamUnavailableError(
            "Could not reach Stripe while processing this event; please redeliver."
        ) from exc
    except Exception as exc:
        db.rollback()
        log.exception("stripe_event_processing_failed", event_id=event_id, type=event_type)
        raise UpstreamUnavailableError(
            "Event processing failed; please redeliver."
        ) from exc

    # Protected too: left outside the try/except this was the one unguarded
    # write on the path, and concurrent redelivery turned a database lock error
    # into a 500 - which Stripe retries, making it a redelivery storm.
    try:
        db.add(StripeEvent(id=event_id, type=event_type))
        db.commit()
    except Exception:
        db.rollback()
        # The work itself already succeeded and every branch is idempotent, so a
        # redelivery is safe; failing to record the id only risks re-applying it.
        log.warning("stripe_event_record_failed", event_id=event_id, type=event_type)

    return MessageOut(message="ok")


@router.get("/subscription", response_model=dict, dependencies=[Depends(rate_limit)])
def subscription_status(user: User = Depends(get_current_user)) -> dict:
    plan = get_plan(user.plan)
    return {
        "plan": plan.key,
        "label": plan.label,
        "price_usd_month": plan.price_usd_month,
        "status": user.subscription_status,
        "current_period_end": user.current_period_end.isoformat() if user.current_period_end else None,
        "billing_enabled": settings.billing_enabled,
        "limits": {
            "max_monitors": plan.max_monitors,
            "min_interval_seconds": plan.min_interval_seconds,
            "max_checks_per_month": plan.max_checks_per_month,
            "history_days": plan.history_days,
        },
    }
