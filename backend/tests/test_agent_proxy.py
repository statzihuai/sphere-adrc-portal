"""Anthropic proxy tests — fully offline (fake streamer + local JWT + temp SQLite).

Verifies: SSE bytes pass through to the client; the four token fields are
captured and the wallet is settled (balance charged, hold released, usage row
settled); a trailing balance event is appended; insufficient credits → 402; and
an upstream error or mid-stream failure releases the hold without charging.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from sphere_backend.app import create_app
from sphere_backend.auth.dependencies import (
    get_anthropic_streamer,
    get_jwks_client,
    get_sessionmaker,
)
from sphere_backend.db import ApiUsageLog, Base, Billing, User, build_engine, build_sessionmaker

D = Decimal

# Canned Anthropic SSE: input 500 + cache_read 4500, final output 1000 → $0.039000 @ sonnet 1.3×
SSE_OK = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"usage":{"input_tokens":500,'
    b'"cache_creation_input_tokens":0,"cache_read_input_tokens":4500,"output_tokens":1}}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n\n'
    b'event: message_delta\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1000}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)
CHARGE = D("0.039000")


class _SigningKey:
    def __init__(self, key):
        self.key = key


class FakeJWKSClient:
    def __init__(self, public_key):
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token):
        return _SigningKey(self._public_key)


@pytest.fixture
def keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, priv.public_key()


@pytest.fixture
def env(tmp_path, keypair):
    priv, pub = keypair
    url = f"sqlite+aiosqlite:///{tmp_path / 'proxy.db'}"

    async def setup() -> int:
        engine = build_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = build_sessionmaker(engine)
        async with sm() as s:
            u = User(workos_user_id="wos_1", email="a@x.edu")
            s.add(u)
            await s.flush()
            s.add(Billing(user_id=u.id, credit_balance_usd=D("10"), reserved_usd=D("0"), trial_used=True))
            await s.commit()
            uid = u.id
        await engine.dispose()
        return uid

    uid = asyncio.run(setup())

    engine = build_engine(url)
    sm = build_sessionmaker(engine)
    app = create_app()
    app.dependency_overrides[get_sessionmaker] = lambda: sm
    app.dependency_overrides[get_jwks_client] = lambda: FakeJWKSClient(pub)
    client = TestClient(app)

    def token(sub="wos_1"):
        now = int(time.time())
        return pyjwt.encode({"sub": sub, "exp": now + 3600, "iat": now}, priv, algorithm="RS256")

    def set_streamer(fn):
        app.dependency_overrides[get_anthropic_streamer] = lambda: fn

    async def state(user_id):
        from sphere_backend.wallet import repository
        async with sm() as s:
            return await repository.get_state(s, user_id)

    async def usage_rows():
        from sqlalchemy import select
        async with sm() as s:
            return (await s.execute(select(ApiUsageLog))).scalars().all()

    yield client, sm, uid, token, set_streamer, state, usage_rows


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_proxy_streams_passthrough_and_settles(env):
    client, sm, uid, token, set_streamer, state, usage_rows = env

    async def fake(body):
        for line in SSE_OK.splitlines(keepends=True):
            yield line

    set_streamer(fake)

    resp = client.post("/v1/agent", json={"messages": [{"role": "user", "content": "hi"}]}, headers=_auth(token()))
    assert resp.status_code == 200
    # upstream SSE passed through, plus our trailing balance event
    assert "message_start" in resp.text and "message_stop" in resp.text
    assert "sphere_balance" in resp.text

    st = asyncio.run(state(uid))
    assert st.reserved_usd == D("0")                    # hold released
    assert st.balance_usd == D("10") - CHARGE           # charged actual

    rows = asyncio.run(usage_rows())
    assert len(rows) == 1 and rows[0].status == "settled"
    assert rows[0].input_tokens == 500 and rows[0].output_tokens == 1000
    assert rows[0].user_charge_usd == CHARGE


def test_proxy_402_when_insufficient(env):
    client, sm, uid, token, set_streamer, state, usage_rows = env

    async def fake(body):
        if False:
            yield b""  # never called — reserve fails first

    set_streamer(fake)
    # drain the balance to (near) zero first via a settled request? Simpler: huge max_tokens
    resp = client.post(
        "/v1/agent",
        json={"messages": [{"role": "user", "content": "x"}], "max_tokens": 100_000_000},
        headers=_auth(token()),
    )
    assert resp.status_code == 402
    assert asyncio.run(state(uid)).reserved_usd == D("0")  # no orphaned hold


def test_proxy_upstream_error_releases_hold(env):
    client, sm, uid, token, set_streamer, state, usage_rows = env

    async def fake(body):
        yield b'event: error\ndata: {"type":"error","status":500}\n\n'

    set_streamer(fake)
    resp = client.post("/v1/agent", json={"messages": [{"role": "user", "content": "x"}]}, headers=_auth(token()))
    assert resp.status_code == 200  # stream opened, error relayed
    st = asyncio.run(state(uid))
    assert st.reserved_usd == D("0")          # hold released (no usage captured)
    assert st.balance_usd == D("10.000000")   # not charged
    rows = asyncio.run(usage_rows())
    assert rows[0].status == "canceled"


def test_proxy_midstream_failure_releases_hold(env):
    client, sm, uid, token, set_streamer, state, usage_rows = env

    async def fake(body):
        yield b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":10}}}\n\n'
        raise RuntimeError("upstream dropped")

    set_streamer(fake)
    with pytest.raises(Exception):
        client.post("/v1/agent", json={"messages": [{"role": "user", "content": "x"}]}, headers=_auth(token()))
    # hold must be released even though usage was incomplete
    assert asyncio.run(state(uid)).reserved_usd == D("0")
    rows = asyncio.run(usage_rows())
    assert rows[0].status == "canceled"


def test_proxy_requires_auth(env):
    client, *_ = env
    assert client.post("/v1/agent", json={"messages": []}).status_code == 401
