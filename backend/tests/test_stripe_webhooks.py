"""Stripe webhook → wallet tests (handle_event on SQLite).

Money-in correctness: credit packs and subscription grants land via the
idempotent wallet credit (exactly-once on Stripe retries), subscription status
transitions are recorded, and events for unknown customers/types are ignored.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.pool import StaticPool

from sphere_backend.billing.payments import handle_event
from sphere_backend.db import Base, Billing, CreditLedger, User, build_sessionmaker
from sphere_backend.db.session import build_engine
from sphere_backend.wallet import repository

D = Decimal


@pytest_asyncio.fixture
async def session():
    engine = build_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = build_sessionmaker(engine)
    async with sm() as s:
        u = User(workos_user_id="wos_1", email="a@x.edu")
        s.add(u)
        await s.flush()
        s.add(
            Billing(
                user_id=u.id,
                stripe_customer_id="cus_1",
                credit_balance_usd=D("0"),
                reserved_usd=D("0"),
                trial_used=True,
            )
        )
        await s.commit()
        s._uid = u.id  # type: ignore[attr-defined]
        yield s
    await engine.dispose()


def _event(etype, obj, eid="evt_1"):
    return {"id": eid, "type": etype, "data": {"object": obj}}


async def _billing(session) -> Billing:
    return await session.get(Billing, session._uid)


# ── credit pack ──────────────────────────────────────────────────────────────
async def test_checkout_payment_credits_pack(session):
    ev = _event(
        "checkout.session.completed",
        {"mode": "payment", "customer": "cus_1", "amount_total": 2500, "payment_intent": "pi_1"},
    )
    assert await handle_event(session, ev) == "checkout.session.completed"
    assert (await repository.get_state(session, session._uid)).balance_usd == D("25.000000")
    rows = (await session.execute(select(CreditLedger).where(CreditLedger.type == "credit_pack"))).scalars().all()
    assert len(rows) == 1 and rows[0].stripe_pi_id == "pi_1"


async def test_pack_prefers_pretax_subtotal(session):
    # amount_total includes tax; credit the pre-tax amount_subtotal
    ev = _event(
        "checkout.session.completed",
        {"mode": "payment", "customer": "cus_1", "amount_subtotal": 2500, "amount_total": 2700},
    )
    await handle_event(session, ev)
    assert (await repository.get_state(session, session._uid)).balance_usd == D("25.000000")


async def test_pack_missing_amount_is_ignored(session):
    ev = _event("checkout.session.completed", {"mode": "payment", "customer": "cus_1"})
    assert await handle_event(session, ev) == "ignored"
    assert (await repository.get_state(session, session._uid)).balance_usd == D("0.000000")


async def test_webhook_credit_is_idempotent_on_event_id(session):
    ev = _event("checkout.session.completed", {"mode": "payment", "customer": "cus_1", "amount_total": 2500})
    await handle_event(session, ev)
    await handle_event(session, ev)  # retry, same event.id
    assert (await repository.get_state(session, session._uid)).balance_usd == D("25.000000")  # not 50
    assert (await session.execute(select(func.count()).select_from(CreditLedger))).scalar_one() == 1


# ── subscription ─────────────────────────────────────────────────────────────
def _invoice(**extra):
    obj = {
        "customer": "cus_1",
        "subscription": "sub_1",
        "lines": {"data": [{"period": {"end": 1893456000}}]},
    }
    obj.update(extra)
    return obj


async def test_invoice_paid_cycle_grants_and_activates(session):
    ev = _event("invoice.payment_succeeded", _invoice(amount_paid=2900, billing_reason="subscription_cycle"))
    assert await handle_event(session, ev) == "invoice.payment_succeeded"
    assert (await repository.get_state(session, session._uid)).balance_usd == D("20.000000")
    b = await _billing(session)
    assert b.sub_status == "active" and b.stripe_sub_id == "sub_1" and b.sub_period_end is not None
    grants = (await session.execute(select(CreditLedger).where(CreditLedger.type == "subscription_grant"))).scalars().all()
    assert len(grants) == 1


async def test_invoice_zero_trial_activates_but_no_grant(session):
    # $0 trial invoice: subscription becomes active, but no $20 credit is granted
    ev = _event("invoice.payment_succeeded", _invoice(amount_paid=0, billing_reason="subscription_create"))
    await handle_event(session, ev)
    assert (await repository.get_state(session, session._uid)).balance_usd == D("0.000000")  # no grant
    assert (await _billing(session)).sub_status == "active"


async def test_invoice_proration_no_grant(session):
    # mid-cycle plan change: paid, but not a create/cycle invoice → no grant
    ev = _event("invoice.payment_succeeded", _invoice(amount_paid=500, billing_reason="subscription_update"))
    await handle_event(session, ev)
    assert (await repository.get_state(session, session._uid)).balance_usd == D("0.000000")


async def test_invoice_failed_sets_past_due(session):
    await handle_event(session, _event("invoice.payment_failed", {"customer": "cus_1"}))
    assert (await _billing(session)).sub_status == "past_due"


async def test_subscription_deleted_sets_canceled(session):
    await handle_event(session, _event("customer.subscription.deleted", {"customer": "cus_1"}))
    assert (await _billing(session)).sub_status == "canceled"


# ── graceful ignores ─────────────────────────────────────────────────────────
async def test_unknown_customer_is_ignored(session):
    ev = _event("checkout.session.completed", {"mode": "payment", "customer": "cus_unknown", "amount_total": 1000})
    assert await handle_event(session, ev) == "ignored"
    assert (await repository.get_state(session, session._uid)).balance_usd == D("0.000000")


async def test_unknown_event_type_is_ignored(session):
    assert await handle_event(session, _event("payment_intent.created", {"customer": "cus_1"})) == "ignored"
