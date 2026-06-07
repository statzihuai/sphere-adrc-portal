"""Reclaim-sweep tests: orphaned pending holds are freed, fresh ones spared.

Exercises ``reclaim_once`` (the unit the background loop calls each tick) — the
production guarantee that a hold left ``pending`` by a disconnected/crashed
request is eventually released.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

from sphere_backend.db import ApiUsageLog, Base, Billing, User, build_sessionmaker
from sphere_backend.db.session import build_engine
from sphere_backend.usage import open_reservation, reclaim_once
from sphere_backend.wallet import repository

D = Decimal


@pytest_asyncio.fixture
async def sessionmaker():
    engine = build_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = build_sessionmaker(engine)
    yield sm
    await engine.dispose()


async def _seed_user(sm, *, balance="10") -> int:
    async with sm() as s:
        u = User(workos_user_id="wos_1", email="a@x.edu")
        s.add(u)
        await s.flush()
        s.add(Billing(user_id=u.id, credit_balance_usd=D(balance), reserved_usd=D("0"), trial_used=True))
        await s.commit()
        return u.id


async def test_reclaim_once_frees_stale_hold_only(sessionmaker):
    uid = await _seed_user(sessionmaker)
    async with sessionmaker() as s:
        await open_reservation(s, user_id=uid, request_id="old", model="m", reserve_amount=D("0.30"))
        await open_reservation(s, user_id=uid, request_id="fresh", model="m", reserve_amount=D("0.20"))
    # backdate the "old" pending row to simulate a disconnected/crashed request
    async with sessionmaker() as s:
        old = (await s.execute(select(ApiUsageLog).where(ApiUsageLog.request_id == "old"))).scalar_one()
        old.created_at = datetime.now(timezone.utc) - timedelta(seconds=3600)
        await s.commit()

    reclaimed = await reclaim_once(sessionmaker, ttl_seconds=1800)
    assert reclaimed == ["old"]

    async with sessionmaker() as s:
        state = await repository.get_state(s, uid)
        assert state.reserved_usd == D("0.200000")  # only the fresh hold remains
        statuses = {
            r.request_id: r.status
            for r in (await s.execute(select(ApiUsageLog))).scalars().all()
        }
    assert statuses == {"old": "canceled", "fresh": "pending"}


async def test_reclaim_once_noop_when_nothing_stale(sessionmaker):
    uid = await _seed_user(sessionmaker)
    async with sessionmaker() as s:
        await open_reservation(s, user_id=uid, request_id="fresh", model="m", reserve_amount=D("0.20"))
    assert await reclaim_once(sessionmaker, ttl_seconds=1800) == []
    async with sessionmaker() as s:
        assert (await repository.get_state(s, uid)).reserved_usd == D("0.200000")
