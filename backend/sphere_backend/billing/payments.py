"""Stripe payment orchestration: customer link + webhook → wallet (design §4.6).

Money-in flows through the wallet's idempotent ``credit`` keyed on the Stripe
``event.id``, so webhook retries grant exactly once. Customers are created
lazily on first billing action (keeps auth/provisioning Stripe-free) and linked
to the local user via ``billing.stripe_customer_id``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Billing
from ..wallet import repository
from .stripe_client import StripeClient

SUBSCRIPTION_GRANT_USD = Decimal("20")          # $20 AI credit per paid period
ALLOWED_PACK_USD = (Decimal("10"), Decimal("25"), Decimal("50"))


async def ensure_customer(
    session: AsyncSession, stripe: StripeClient, *, user_id: int, email: str
) -> str:
    """Return the user's Stripe customer id, creating it once if missing.

    Locks the billing row so concurrent checkouts can't create two customers.
    """
    result = await session.execute(
        select(Billing).where(Billing.user_id == user_id).with_for_update()
    )
    billing = result.scalar_one()
    if billing.stripe_customer_id:
        return billing.stripe_customer_id
    customer_id = stripe.create_customer(email=email, metadata={"sphere_user_id": str(user_id)})
    billing.stripe_customer_id = customer_id
    await session.commit()
    return customer_id


async def _user_id_by_customer(session: AsyncSession, customer_id: str | None) -> int | None:
    if not customer_id:
        return None
    result = await session.execute(
        select(Billing.user_id).where(Billing.stripe_customer_id == customer_id)
    )
    return result.scalar_one_or_none()


def _ts_to_dt(ts) -> datetime | None:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None


def _invoice_period_end(invoice: Mapping[str, Any]) -> datetime | None:
    try:
        return _ts_to_dt(invoice["lines"]["data"][0]["period"]["end"])
    except (KeyError, IndexError, TypeError):
        return None


async def _set_subscription(
    session: AsyncSession,
    customer_id: str | None,
    *,
    status: str,
    sub_id: str | None = None,
    period_end: datetime | None = None,
) -> None:
    result = await session.execute(
        select(Billing).where(Billing.stripe_customer_id == customer_id).with_for_update()
    )
    billing = result.scalar_one_or_none()
    if billing is None:
        return
    billing.sub_status = status
    if sub_id is not None:
        billing.stripe_sub_id = sub_id
    if period_end is not None:
        billing.sub_period_end = period_end
    await session.commit()


async def handle_event(session: AsyncSession, event: Mapping[str, Any]) -> str:
    """Apply a verified Stripe event. Returns the handled type, or ``ignored``."""
    etype = event["type"]
    obj = event["data"]["object"]
    event_id = event["id"]
    customer_id = obj.get("customer")

    if etype == "checkout.session.completed" and obj.get("mode") == "payment":
        user_id = await _user_id_by_customer(session, customer_id)
        if user_id is None:
            return "ignored"
        amount = Decimal(obj["amount_total"]) / 100  # cents → dollars
        await repository.credit(
            session,
            user_id,
            amount,
            type="credit_pack",
            idempotency_key=event_id,
            stripe_pi_id=obj.get("payment_intent"),
        )
        return etype

    if etype == "invoice.payment_succeeded":
        user_id = await _user_id_by_customer(session, customer_id)
        if user_id is None:
            return "ignored"
        await repository.credit(
            session,
            user_id,
            SUBSCRIPTION_GRANT_USD,
            type="subscription_grant",
            idempotency_key=event_id,
        )
        await _set_subscription(
            session,
            customer_id,
            status="active",
            sub_id=obj.get("subscription"),
            period_end=_invoice_period_end(obj),
        )
        return etype

    if etype == "invoice.payment_failed":
        await _set_subscription(session, customer_id, status="past_due")
        return etype

    if etype == "customer.subscription.deleted":
        await _set_subscription(session, customer_id, status="canceled")
        return etype

    return "ignored"
