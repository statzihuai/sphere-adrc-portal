"""Data-layer + provisioning tests (async, SQLite in-memory).

Covers BACKEND_DESIGN.md §4.3 step 3: first login creates user + billing + a
single $10 trial-grant ledger row; repeat/idempotent calls don't re-grant;
distinct WorkOS ids get their own accounts; the unique idempotency key blocks a
double grant; and money round-trips through the USD column with no float drift.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from sphere_backend.db import Base, Billing, CreditLedger, User, build_sessionmaker
from sphere_backend.db.session import build_engine
from sphere_backend.users import TRIAL_GRANT_USD, provision_user


@pytest_asyncio.fixture
async def session():
    # In-memory SQLite needs a single shared connection (StaticPool) so DDL and
    # queries see the same database for the test's lifetime.
    engine = build_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = build_sessionmaker(engine)
    async with sm() as s:
        yield s
    await engine.dispose()


async def _ledger_count(session) -> int:
    return (await session.execute(select(func.count()).select_from(CreditLedger))).scalar_one()


async def test_provision_creates_user_billing_and_trial(session):
    user = await provision_user(session, workos_user_id="wos_1", email="a@x.edu")
    assert user.id is not None
    assert user.workos_user_id == "wos_1"
    assert user.email == "a@x.edu"

    billing = await session.get(Billing, user.id)
    assert billing.credit_balance_usd == Decimal("10.000000")
    assert billing.reserved_usd == Decimal("0.000000")
    assert billing.trial_used is True

    rows = (await session.execute(select(CreditLedger))).scalars().all()
    assert len(rows) == 1
    entry = rows[0]
    assert entry.type == "trial_grant"
    assert entry.delta_usd == Decimal("10.000000")
    assert entry.balance_after == Decimal("10.000000")
    assert entry.idempotency_key == f"trial:{user.id}"


async def test_provision_is_idempotent(session):
    first = await provision_user(session, workos_user_id="wos_1", email="a@x.edu")
    again = await provision_user(session, workos_user_id="wos_1", email="a@x.edu")
    assert again.id == first.id
    # no second user, no second trial
    assert (await session.execute(select(func.count()).select_from(User))).scalar_one() == 1
    assert await _ledger_count(session) == 1
    billing = await session.get(Billing, first.id)
    assert billing.credit_balance_usd == Decimal("10.000000")  # not 20


async def test_distinct_workos_ids_get_separate_accounts(session):
    u1 = await provision_user(session, workos_user_id="wos_1", email="a@x.edu")
    u2 = await provision_user(session, workos_user_id="wos_2", email="b@y.edu")
    assert u1.id != u2.id
    assert await _ledger_count(session) == 2


async def test_duplicate_idempotency_key_is_rejected(session):
    user = await provision_user(session, workos_user_id="wos_1", email="a@x.edu")
    # A second ledger row reusing the trial key must violate the unique index.
    session.add(
        CreditLedger(
            user_id=user.id,
            delta_usd=Decimal("10"),
            balance_after=Decimal("20"),
            type="trial_grant",
            idempotency_key=f"trial:{user.id}",
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_usd_column_is_decimal_exact(session):
    user = await provision_user(session, workos_user_id="wos_1", email="a@x.edu")
    billing = await session.get(Billing, user.id)
    billing.credit_balance_usd = Decimal("0.123456")
    await session.commit()
    refreshed = await session.get(Billing, user.id)
    assert refreshed.credit_balance_usd == Decimal("0.123456")
    assert isinstance(refreshed.credit_balance_usd, Decimal)
