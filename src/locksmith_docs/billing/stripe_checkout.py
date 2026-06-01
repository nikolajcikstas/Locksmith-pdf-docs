from __future__ import annotations

import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from locksmith_docs.billing.plans import Plan


class StripeNotConfigured(RuntimeError):
    pass


def create_checkout_session(plan: Plan, success_url: str, cancel_url: str) -> str:
    secret_key = os.environ.get("STRIPE_SECRET_KEY")
    price_id = plan.stripe_price_id
    if not secret_key or not price_id:
        raise StripeNotConfigured("Stripe secret key or plan price id is not configured.")

    payload = urlencode(
        {
            "mode": "subscription",
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": "1",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "allow_promotion_codes": "true",
            "billing_address_collection": "auto",
            "metadata[plan_id]": plan.id,
        }
    ).encode("utf-8")
    request = Request(
        "https://api.stripe.com/v1/checkout/sessions",
        data=payload,
        headers={
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urlopen(request, timeout=45) as response:
        data = json.loads(response.read().decode("utf-8"))
    checkout_url = data.get("url")
    if not checkout_url:
        raise RuntimeError("Stripe did not return a checkout URL.")
    return str(checkout_url)
