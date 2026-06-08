"""Billing HTTP endpoints — offline (fake Stripe + local JWT + temp SQLite)."""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import update

from sphere_backend.app import create_app
from sphere_backend.auth.dependencies import get_jwks_client, get_sessionmaker, get_stripe_client
from sphere_backend.billing.stripe_client import WebhookVerificationError
from sphere_backend.config import Settings
from sphere_backend.db import Base, Billing, User, build_engine, build_sessionmaker

D = Decimal


class _Key:
    def __init__(self, k):
        self.key = k


class FakeJWKS:
    def __init__(self, pub):
        self._pub = pub

    def get_signing_key_from_jwt(self, token):
        return _Key(self._pub)


class FakeStripe:
    def __init__(self):
        self.fail_sig = False

    def create_customer(self, *, email, metadata):
        return "cus_fake"

    def create_payment_checkout(self, **kw):
        return "https://checkout.test/pay"

    def create_subscription_checkout(self, **kw):
        return "https://checkout.test/sub"

    def create_portal_session(self, **kw):
        return "https://billing.test/portal"

    def verify_webhook(self, *, payload, sig_header):
        if self.fail_sig:
            raise WebhookVerificationError("bad signature")
        return json.loads(payload)


@pytest.fixture
def keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, priv.public_key()


@pytest.fixture
def env(tmp_path, keypair):
    priv, pub = keypair
    url = f"sqlite+aiosqlite:///{tmp_path / 'billing.db'}"

    async def setup() -> int:
        engine = build_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = build_sessionmaker(engine)
        async with sm() as s:
            u = User(workos_user_id="wos_1", email="a@x.edu")
            s.add(u)
            await s.flush()
            s.add(Billing(user_id=u.id, credit_balance_usd=D("5"), reserved_usd=D("0"), trial_used=True))
            await s.commit()
            uid = u.id
        await engine.dispose()
        return uid

    uid = asyncio.run(setup())
    engine = build_engine(url)
    sm = build_sessionmaker(engine)
    stripe = FakeStripe()

    app = create_app(Settings(app_env="test", stripe_price_subscription="price_sub"))
    app.dependency_overrides[get_sessionmaker] = lambda: sm
    app.dependency_overrides[get_jwks_client] = lambda: FakeJWKS(pub)
    app.dependency_overrides[get_stripe_client] = lambda: stripe
    client = TestClient(app)

    def token(sub="wos_1"):
        now = int(time.time())
        return pyjwt.encode({"sub": sub, "exp": now + 3600, "iat": now}, priv, algorithm="RS256")

    async def link_customer(cid="cus_fake"):
        async with sm() as s:
            await s.execute(update(Billing).where(Billing.user_id == uid).values(stripe_customer_id=cid))
            await s.commit()

    async def get_billing():
        async with sm() as s:
            return await s.get(Billing, uid)

    yield client, stripe, uid, token, link_customer, get_billing


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_checkout_pack_creates_customer_and_returns_url(env):
    client, stripe, uid, token, link, get_billing = env
    r = client.post("/billing/checkout/pack", json={"amount_usd": "25"}, headers=_auth(token()))
    assert r.status_code == 200
    assert r.json()["url"] == "https://checkout.test/pay"
    assert asyncio.run(get_billing()).stripe_customer_id == "cus_fake"


def test_checkout_pack_rejects_bad_amount(env):
    client, *_, token, _, _ = env
    assert client.post("/billing/checkout/pack", json={"amount_usd": "7"}, headers=_auth(token())).status_code == 400


def test_subscribe_returns_url(env):
    client, *_, token, _, _ = env
    r = client.post("/billing/checkout/subscribe", headers=_auth(token()))
    assert r.status_code == 200 and r.json()["url"] == "https://checkout.test/sub"


def test_portal_returns_url(env):
    client, *_, token, _, _ = env
    r = client.post("/billing/portal", headers=_auth(token()))
    assert r.status_code == 200 and r.json()["url"] == "https://billing.test/portal"


def test_balance_reflects_wallet(env):
    client, *_, token, _, _ = env
    r = client.get("/billing/balance", headers=_auth(token()))
    assert r.status_code == 200 and r.json()["balance_usd"] == "5.000000"


def test_webhook_credits_wallet(env):
    client, stripe, uid, token, link, get_billing = env
    asyncio.run(link())  # link the seeded user to cus_fake
    event = {
        "id": "evt_http_1",
        "type": "checkout.session.completed",
        "data": {"object": {"mode": "payment", "customer": "cus_fake", "amount_total": 1000}},
    }
    r = client.post("/billing/webhook", json=event, headers={"Stripe-Signature": "t=1,v1=sig"})
    assert r.status_code == 200 and r.json()["status"] == "checkout.session.completed"
    assert client.get("/billing/balance", headers=_auth(token())).json()["balance_usd"] == "15.000000"


def test_webhook_bad_signature_rejected(env):
    client, stripe, *_ = env
    stripe.fail_sig = True
    r = client.post("/billing/webhook", json={"id": "x", "type": "y", "data": {"object": {}}},
                    headers={"Stripe-Signature": "bad"})
    assert r.status_code == 400


def test_billing_requires_auth(env):
    client, *_ = env
    assert client.get("/billing/balance").status_code == 401
