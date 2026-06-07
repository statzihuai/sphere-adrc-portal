"""DB-backed wallet adapter — logic tests on in-memory SQLite.

Covers the load→apply→persist→ledger behavior of reserve/settle/credit/deduct.
SQLite ignores ``FOR UPDATE`` (no row locking), so true concurrency is verified
separately in test_wallet_concurrency.py against Postgres; here we verify the
money math, persistence, and idempotency.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.pool import StaticPool

from sphere_backend.db import Base, Billing, CreditLedger, User, build_sessionmaker
from sphere_backend.db.session import build_engine
from sphere_backend.wallet import InsufficientCreditsError, repository

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
        yield s
    await engine.dispose()


async def _make_user(session, *, balance="10", reserved="0", wid="wos_1") -> int:
    user = User(workos_user_id=wid, email="a@x.edu")
    session.add(user)
    await session.flush()
    session.add(
        Billing(
            user_id=user.id,
            credit_balance_usd=D(balance),
            reserved_usd=D(reserved),
            trial_used=True,
        )
    )
    await session.commit()
    return user.id


async def _ledger_count(session) -> int:
    return (await session.execute(select(func.count()).select_from(CreditLedger))).scalar_one()


# ── reserve ──────────────────────────────────────────────────────────────────
async def test_reserve_holds_and_persists(session):
    uid = await _make_user(session, balance="10")
    held = await repository.reserve(session, uid, D("0.20"))
    assert held == D("0.200000")
    state = await repository.get_state(session, uid)
    assert state.reserved_usd == D("0.200000")
    assert state.balance_usd == D("10.000000")
    assert state.available_usd == D("9.800000")


async def test_reserve_insufficient_raises_and_leaves_state_unchanged(session):
    uid = await _make_user(session, balance="0.10")
    with pytest.raises(InsufficientCreditsError):
        await repository.reserve(session, uid, D("0.20"))
    state = await repository.get_state(session, uid)
    assert state.balance_usd == D("0.100000")
    assert state.reserved_usd == D("0")


# ── settle ──────────────────────────────────────────────────────────────────
async def test_settle_releases_hold_deducts_and_writes_ledger(session):
    uid = await _make_user(session, balance="10")
    await repository.reserve(session, uid, D("0.20"))
    entry = await repository.settle(session, uid, reserve_amount=D("0.20"), actual_charge=D("0.03"))
    assert entry.type == "ai_usage"
    state = await repository.get_state(session, uid)
    assert state.reserved_usd == D("0")
    assert state.balance_usd == D("9.970000")
    rows = (await session.execute(select(CreditLedger))).scalars().all()
    assert len(rows) == 1
    assert rows[0].delta_usd == D("-0.030000")
    assert rows[0].balance_after == D("9.970000")


# ── credit (idempotent) ──────────────────────────────────────────────────────
async def test_credit_adds_funds(session):
    uid = await _make_user(session, balance="0")
    entry = await repository.credit(session, uid, D("25"), type="credit_pack")
    assert entry.delta_usd == D("25.000000")
    assert (await repository.get_state(session, uid)).balance_usd == D("25.000000")


async def test_credit_is_idempotent_on_key(session):
    uid = await _make_user(session, balance="0")
    key = "stripe_evt_123"
    first = await repository.credit(session, uid, D("25"), type="credit_pack", idempotency_key=key)
    second = await repository.credit(session, uid, D("25"), type="credit_pack", idempotency_key=key)
    assert first is not None
    assert second is None  # already applied → no-op
    assert (await repository.get_state(session, uid)).balance_usd == D("25.000000")  # not 50
    assert await _ledger_count(session) == 1


# ── deduct ───────────────────────────────────────────────────────────────────
async def test_deduct_known_cost(session):
    uid = await _make_user(session, balance="5")
    await repository.deduct(session, uid, D("0.10"), type="data_egress")
    assert (await repository.get_state(session, uid)).balance_usd == D("4.900000")


async def test_deduct_floor_fails_closed(session):
    uid = await _make_user(session, balance="0.05")
    with pytest.raises(InsufficientCreditsError):
        await repository.deduct(session, uid, D("0.10"), type="data_egress", min_balance=D("0"))
    assert (await repository.get_state(session, uid)).balance_usd == D("0.050000")


async def test_missing_account_raises(session):
    from sphere_backend.wallet import WalletAccountNotFound

    with pytest.raises(WalletAccountNotFound):
        await repository.get_state(session, 999)
