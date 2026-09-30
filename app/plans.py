"""Plan definitions and quota enforcement.

Prices here are the source of truth for what the app *shows*. Stripe is the
source of truth for what is actually *charged* - the two are linked by the
STRIPE_PRICE_* environment variables.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import Plan


@dataclass(frozen=True)
class PlanSpec:
    key: str
    label: str
    price_usd_month: int
    max_monitors: int
    min_interval_seconds: int
    max_checks_per_month: int
    webhook_enabled: bool
    history_days: int


PLANS: dict[str, PlanSpec] = {
    Plan.free.value: PlanSpec(
        key="free",
        label="Free",
        price_usd_month=0,
        max_monitors=3,
        min_interval_seconds=3600,      # hourly
        max_checks_per_month=2_200,
        webhook_enabled=True,
        history_days=7,
    ),
    Plan.pro.value: PlanSpec(
        key="pro",
        label="Pro",
        price_usd_month=19,
        max_monitors=50,
        min_interval_seconds=300,       # 5 minutes
        max_checks_per_month=450_000,
        webhook_enabled=True,
        history_days=90,
    ),
    Plan.business.value: PlanSpec(
        key="business",
        label="Business",
        price_usd_month=79,
        max_monitors=300,
        min_interval_seconds=60,        # 1 minute
        max_checks_per_month=5_000_000,
        webhook_enabled=True,
        history_days=365,
    ),
}

DEFAULT_PLAN = Plan.free.value


def get_plan(key: str | None) -> PlanSpec:
    return PLANS.get(key or DEFAULT_PLAN, PLANS[DEFAULT_PLAN])
