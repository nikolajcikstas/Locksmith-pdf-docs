from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    description: str
    price_cents: int
    stripe_price_env: str
    features: tuple[str, ...]

    @property
    def price_label(self) -> str:
        return f"${self.price_cents // 100}/mo"

    @property
    def stripe_price_id(self) -> str:
        return os.environ.get(self.stripe_price_env, "")


PLANS = (
    Plan(
        id="starter",
        name="Starter",
        description="For occasional vehicle lookups and single-technician use.",
        price_cents=500,
        stripe_price_env="STRIPE_PRICE_STARTER",
        features=("Vehicle lookup", "Structured reports", "Blog and training videos"),
    ),
    Plan(
        id="pro",
        name="Pro",
        description="For active locksmiths who need faster field reference access.",
        price_cents=1000,
        stripe_price_env="STRIPE_PRICE_PRO",
        features=("Everything in Starter", "Expanded technical references", "Priority catalog updates"),
    ),
    Plan(
        id="shop",
        name="Shop",
        description="For small teams that share one shop workflow.",
        price_cents=1500,
        stripe_price_env="STRIPE_PRICE_SHOP",
        features=("Everything in Pro", "Team-ready account structure", "Early access to new imports"),
    ),
)


def get_plan(plan_id: str) -> Plan | None:
    return next((plan for plan in PLANS if plan.id == plan_id), None)


def subscription_bypass_enabled() -> bool:
    return os.environ.get("DEV_SUBSCRIPTION_BYPASS", "1") == "1"
