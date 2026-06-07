"""Reservation lifecycle tests (open → finalize/cancel, reclaim) on SQLite.

Verifies the exactly-once hold release: finalize settles + charges, cancel
releases without charge, both are idempotent, and reclaim_stale sweeps crashed
pending reservations while leaving fresh ones alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.pool import StaticPool

from sphere_backend.billing import Usage, get_rate
from sphere_backend.db import ApiUsageLog, Base, Billing, CreditLedger, User, build_sessionmaker
from sphere_backend.db.session import build_engine
from sphere_backend.usage import cancel, finalize, open_reservation, reclaim_stale
from sphere_backend.wallet import InsufficientCreditsError, repository

D = Decimal
SONNET = get_rate("claude-sonnet-4-6")
# Usage(input=500, cache_read=4500, output=1000) → user_charge $0.039000 at 1.3×
WARM = Usage(input_tokens=500, cache_creation_tokens=0, cache_read_tokens=4500, output_tokens=1000)
CHARGE = D("0.039000")


@pytest_asyncio.fixture
async def session():
    engine = build_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = build_sessionmaker(engine)
    async with sm() as s:
        yield s
    await engine.dispose()


async def _make_user(session, *, balance="10") -> int:
    user = User(workos_user_id="wos_1", email="a@x.edu")
    session.add(user)
    await session.flush()
    session.add(Billing(user_id=user.id, credit_balance_usd=D(balance), reserved_usd=D("0"), trial_used=True))
    await session.commit()
    return user.id


# ── open_reservation ─────────────────────────────────────────────────────────
async def test_open_reservation_holds_and_creates_pending_row(session):
    uid = await _make_user(session)
    row = await open_reservation(
        session, user_id=uid, request_id="req_1", model="claude-sonnet-4-6", reserve_amount=D("0.50")
    )
    assert row.status == "pending"
    assert row.reserved_usd == D("0.500000")
    state = await repository.get_state(session, uid)
    assert state.reserved_usd == D("0.500000")
    assert state.balance_usd == D("10.000000")  # hold only — not charged yet


async def test_open_reservation_insufficient_raises(session):
    uid = await _make_user(session, balance="0.10")
    with pytest.raises(InsufficientCreditsError):
        await open_reservation(
            session, user_id=uid, request_id="req_1", model="m", reserve_amount=D("0.50")
        )
    state = await repository.get_state(session, uid)
    assert state.reserved_usd == D("0")


# ── finalize ─────────────────────────────────────────────────────────────────
async def test_finalize_settles_charges_and_logs_usage(session):
    uid = await _make_user(session)
    await open_reservation(session, user_id=uid, request_id="req_1", model="claude-sonnet-4-6", reserve_amount=D("0.50"))
    row = await finalize(session, request_id="req_1", usage=WARM, rate=SONNET)
    assert row.status == "settled"
    assert row.input_tokens == 500 and row.cache_read_tokens == 4500 and row.output_tokens == 1000
    assert row.billed_input_tokens == 5000
    assert row.user_charge_usd == CHARGE
    assert row.margin_usd == row.user_charge_usd - row.sphere_cost_usd

    state = await repository.get_state(session, uid)
    assert state.reserved_usd == D("0")            # hold released
    assert state.balance_usd == D("10") - CHARGE   # charged the actual amount

    ledger = (await session.execute(select(CreditLedger).where(CreditLedger.type == "ai_usage"))).scalars().all()
    assert len(ledger) == 1
    assert ledger[0].delta_usd == -CHARGE
    assert ledger[0].api_usage_id == row.id


async def test_finalize_is_idempotent(session):
    uid = await _make_user(session)
    await open_reservation(session, user_id=uid, request_id="req_1", model="m", reserve_amount=D("0.50"))
    await finalize(session, request_id="req_1", usage=WARM, rate=SONNET)
    again = await finalize(session, request_id="req_1", usage=WARM, rate=SONNET)
    assert again is None  # no double-charge
    state = await repository.get_state(session, uid)
    assert state.balance_usd == D("10") - CHARGE
    assert (await session.execute(select(func.count()).select_from(CreditLedger))).scalar_one() == 1


# ── cancel ───────────────────────────────────────────────────────────────────
async def test_cancel_releases_hold_without_charge(session):
    uid = await _make_user(session)
    await open_reservation(session, user_id=uid, request_id="req_1", model="m", reserve_amount=D("0.50"))
    row = await cancel(session, request_id="req_1")
    assert row.status == "canceled"
    state = await repository.get_state(session, uid)
    assert state.reserved_usd == D("0")
    assert state.balance_usd == D("10.000000")  # untouched
    assert (await session.execute(select(func.count()).select_from(CreditLedger))).scalar_one() == 0


async def test_cancel_then_finalize_is_noop(session):
    uid = await _make_user(session)
    await open_reservation(session, user_id=uid, request_id="req_1", model="m", reserve_amount=D("0.50"))
    await cancel(session, request_id="req_1")
    assert await finalize(session, request_id="req_1", usage=WARM, rate=SONNET) is None
    assert (await repository.get_state(session, uid)).balance_usd == D("10.000000")


# ── reclaim ──────────────────────────────────────────────────────────────────
async def test_reclaim_stale_cancels_old_pending_only(session):
    uid = await _make_user(session)
    await open_reservation(session, user_id=uid, request_id="old", model="m", reserve_amount=D("0.30"))
    await open_reservation(session, user_id=uid, request_id="fresh", model="m", reserve_amount=D("0.20"))
    # backdate the "old" row's created_at to simulate a crashed request
    old = (await session.execute(select(ApiUsageLog).where(ApiUsageLog.request_id == "old"))).scalar_one()
    old.created_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    await session.commit()

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
    reclaimed = await reclaim_stale(session, older_than=cutoff)
    assert reclaimed == ["old"]

    # old hold released; fresh hold intact
    state = await repository.get_state(session, uid)
    assert state.reserved_usd == D("0.200000")
