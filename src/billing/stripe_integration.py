"""
Stripe billing: checkout, customer portal, webhooks, and an operator CLI.

.env:
    STRIPE_SECRET_KEY=sk_live_...        (or sk_test_...)
    STRIPE_WEBHOOK_SECRET=whsec_...      (from the webhook endpoint in the Stripe dashboard)
    STRIPE_PRICE_PRO=price_...           (recurring monthly price ids)
    STRIPE_PRICE_ENTERPRISE=price_...
    APP_BASE_URL=https://app.example.com (where Stripe sends people back to)

Flow:
    /billing/checkout  -> Stripe Checkout (subscription) with the user id in metadata
    webhook            -> checkout.session.completed / customer.subscription.* set the tier
    /billing/portal    -> Stripe Customer Portal for card changes and cancellation

Operator CLI (no Stripe needed, e.g. for your own account or a client you invoice by hand):
    python3 -m src.billing.stripe_integration set-tier you@example.com enterprise
    python3 -m src.billing.stripe_integration list
"""

import logging
import os
import sys
from datetime import datetime

from config.db import get_connection
from src.billing.tiers import TIER_ORDER, is_valid_tier

log = logging.getLogger("billing")


class BillingError(Exception):
    """Stripe is not configured, refused the request, or sent something we cannot verify."""


def _cfg(name: str) -> str:
    return (os.getenv(name) or "").strip()


def is_configured() -> bool:
    key = _cfg("STRIPE_SECRET_KEY")
    return bool(key) and not key.startswith("your_")


def price_for_tier(tier: str) -> str:
    price = _cfg({"pro": "STRIPE_PRICE_PRO", "enterprise": "STRIPE_PRICE_ENTERPRISE"}.get(tier, ""))
    if not price or price.startswith("your_"):
        raise BillingError(f"No Stripe price configured for the {tier} plan.")
    return price


def tier_for_price(price_id: str) -> str | None:
    for tier in ("pro", "enterprise"):
        try:
            if price_for_tier(tier) == price_id:
                return tier
        except BillingError:
            continue
    return None


def _stripe():
    if not is_configured():
        raise BillingError("Billing is not configured yet (STRIPE_SECRET_KEY missing).")
    import stripe
    stripe.api_key = _cfg("STRIPE_SECRET_KEY")
    return stripe


# ---------------------------------------------------------------------------
# Database side
# ---------------------------------------------------------------------------

def set_tier(user_id: int, tier: str, customer_id: str | None = None,
             subscription_id: str | None = None) -> None:
    if not is_valid_tier(tier):
        raise BillingError(f"Unknown tier {tier!r}. Choose from: {', '.join(TIER_ORDER)}")
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET tier = ?, stripe_customer_id = COALESCE(?, stripe_customer_id), "
            "stripe_subscription_id = COALESCE(?, stripe_subscription_id) WHERE id = ?",
            (tier, customer_id, subscription_id, user_id))


def user_by_customer(customer_id: str) -> dict | None:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM users WHERE stripe_customer_id = ?", (customer_id,)).fetchone()


def record_event(event_id: str, event_type: str, user_id: int | None, tier: str | None) -> bool:
    """Store a webhook event id. Returns False if it was already processed (Stripe retries)."""
    with get_connection() as conn:
        if conn.execute("SELECT 1 AS x FROM billing_events WHERE stripe_event_id = ?", (event_id,)).fetchone():
            return False
        conn.execute(
            "INSERT INTO billing_events (user_id, stripe_event_id, event_type, tier, received_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, event_id, event_type, tier, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    return True


# ---------------------------------------------------------------------------
# Stripe calls
# ---------------------------------------------------------------------------

def create_checkout_session(user: dict, tier: str, success_url: str, cancel_url: str) -> str:
    """Return the Stripe Checkout URL to redirect the user to."""
    if tier not in ("pro", "enterprise"):
        raise BillingError("Choose the Pro or Enterprise plan.")
    stripe = _stripe()
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_for_tier(tier), "quantity": 1}],
            customer=user.get("stripe_customer_id") or None,
            customer_email=None if user.get("stripe_customer_id") else user["email"],
            client_reference_id=str(user["id"]),
            metadata={"user_id": str(user["id"]), "tier": tier},
            subscription_data={"metadata": {"user_id": str(user["id"]), "tier": tier}},
            success_url=success_url, cancel_url=cancel_url,
            allow_promotion_codes=True,
        )
    except stripe.error.StripeError as exc:          # type: ignore[attr-defined]
        raise BillingError(f"Stripe refused to start checkout: {exc.user_message or exc}") from exc
    return session.url


def create_portal_session(user: dict, return_url: str) -> str:
    stripe = _stripe()
    if not user.get("stripe_customer_id"):
        raise BillingError("No billing account yet. Subscribe to a plan first.")
    try:
        session = stripe.billing_portal.Session.create(customer=user["stripe_customer_id"],
                                                       return_url=return_url)
    except stripe.error.StripeError as exc:          # type: ignore[attr-defined]
        raise BillingError(f"Stripe refused to open the portal: {exc.user_message or exc}") from exc
    return session.url


def handle_webhook(payload: bytes, signature: str) -> dict:
    """
    Verify and apply a Stripe webhook. Returns {"handled": bool, "type": ..., "tier": ...}.
    Raises BillingError on a bad signature (respond 400 so Stripe retries nothing).
    """
    stripe = _stripe()
    secret = _cfg("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise BillingError("STRIPE_WEBHOOK_SECRET is not set.")
    try:
        event = stripe.Webhook.construct_event(payload, signature, secret)
    except (ValueError, stripe.error.SignatureVerificationError) as exc:   # type: ignore[attr-defined]
        raise BillingError(f"Webhook rejected: {exc}") from exc
    return apply_event(event)


def apply_event(event) -> dict:
    """Update the user's tier from an already-verified Stripe event object or dict."""
    data = event["data"]["object"]
    etype = event["type"]
    event_id = event.get("id", "")
    result = {"handled": False, "type": etype, "tier": None, "user_id": None}

    if etype == "checkout.session.completed":
        user_id = int((data.get("metadata") or {}).get("user_id") or data.get("client_reference_id") or 0)
        tier = (data.get("metadata") or {}).get("tier")
        if user_id and tier and is_valid_tier(tier):
            if record_event(event_id, etype, user_id, tier):
                set_tier(user_id, tier, data.get("customer"), data.get("subscription"))
            result.update(handled=True, tier=tier, user_id=user_id)

    elif etype in ("customer.subscription.updated", "customer.subscription.created"):
        user = user_by_customer(data.get("customer", ""))
        meta_uid = (data.get("metadata") or {}).get("user_id")
        user_id = user["id"] if user else (int(meta_uid) if meta_uid else None)
        if user_id:
            items = (data.get("items") or {}).get("data") or []
            price_id = ((items[0].get("price") or {}).get("id")) if items else None
            active = data.get("status") in ("active", "trialing", "past_due")
            tier = (tier_for_price(price_id) or (data.get("metadata") or {}).get("tier")) if active else "free"
            if tier and is_valid_tier(tier):
                if record_event(event_id, etype, user_id, tier):
                    set_tier(user_id, tier, data.get("customer"), data.get("id"))
                result.update(handled=True, tier=tier, user_id=user_id)

    elif etype == "customer.subscription.deleted":
        user = user_by_customer(data.get("customer", ""))
        if user:
            if record_event(event_id, etype, user["id"], "free"):
                set_tier(user["id"], "free")
            result.update(handled=True, tier="free", user_id=user["id"])

    return result


# ---------------------------------------------------------------------------
# Operator CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["set-tier"] and len(argv) == 3:
        email, tier = argv[1].lower(), argv[2].lower()
        with get_connection() as conn:
            user = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if not user:
            print(f"No user with email {email}", file=sys.stderr)
            return 1
        try:
            set_tier(user["id"], tier)
        except BillingError as exc:
            print(exc, file=sys.stderr)
            return 2
        print(f"{email} is now on the {tier} plan.")
        return 0
    if argv[:1] == ["list"]:
        with get_connection() as conn:
            for u in conn.execute("SELECT email, tier, created_at FROM users ORDER BY id"):
                print(f"{u['email']:<40} {u['tier']:<12} since {u['created_at']}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
