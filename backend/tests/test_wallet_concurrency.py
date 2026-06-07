"""True-concurrency tests against real Postgres (SELECT … FOR UPDATE).

Gated on ``SPHERE_TEST_DATABASE_URL`` (a ``postgresql+asyncpg://…`` DSN) — skipped
where Postgres isn't available, so the default suite stays portable. These prove
the invariants SQLite can only approximate: two concurrent reserves can't
double-spend, and two concurrent first-logins yield one user + one trial grant.
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from sphere_backend.db import Base, Billing, CreditLedger, User, build_sessionmaker
from sphere_backend.db.session import build_engine
from sphere_backend.users import provision_user
from sphere_backend.wallet import InsufficientCreditsError, repository

PG_URL = os.environ.get("SPHERE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="set SPHERE_TEST_DATABASE_URL to a postgresql+asyncpg:// DSN"
)

D = Decimal


@pytest_asyncio.fixture
async def sessionmaker():
    engine = build_engine(PG_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    sm = build_sessionmaker(engine)
    yield sm
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _seed_user(sm, balance: str) -> int:
    async with sm() as s:
        user = User(workos_user_id="wos_seed", email="a@x.edu")
        s.add(user)
        await s.flush()
        s.add(
            Billing(
                user_id=user.id,
                credit_balance_usd=D(balance),
                reserved_usd=D("0"),
                trial_used=True,
            )
        )
        await s.commit()
        return user.id


async def test_concurrent_reserves_no_double_spend(sessionmaker):
    # balance only covers ONE 0.20 hold; two fire at once.
    uid = await _seed_user(sessionmaker, "0.30")

    async def attempt() -> str:
        async with sessionmaker() as s:
            try:
                await repository.reserve(s, uid, D("0.20"))
                return "ok"
            except InsufficientCreditsError:
                return "402"

    results = await asyncio.gather(attempt(), attempt())
    assert sorted(results) == ["402", "ok"]  # exactly one wins

    async with sessionmaker() as s:
        state = await repository.get_state(s, uid)
        assert state.reserved_usd == D("0.200000")  # only one hold landed
        assert state.balance_usd == D("0.300000")


async def test_concurrent_first_logins_one_user_one_grant(sessionmaker):
    async def attempt():
        async with sessionmaker() as s:
            await provision_user(s, workos_user_id="wos_race", email="race@x.edu")

    await asyncio.gather(attempt(), attempt(), attempt())

    async with sessionmaker() as s:
        users = (await s.execute(select(func.count()).select_from(User))).scalar_one()
        grants = (
            await s.execute(
                select(func.count())
                .select_from(CreditLedger)
                .where(CreditLedger.type == "trial_grant")
            )
        ).scalar_one()
    assert users == 1
    assert grants == 1  # exactly-once trial despite the race
