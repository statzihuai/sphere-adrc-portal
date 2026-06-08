"""Auth endpoint + JWT verification tests — fully offline.

WorkOS is replaced by a fake ``AuthProvider``; access tokens are minted with a
locally-generated RSA keypair and verified through a fake JWKS client, so no
WorkOS account or network is needed. The DB is a temp-file SQLite the app's async
engine shares with the sync engine that creates the tables.
"""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from sphere_backend.api.auth import _STATE_COOKIE
from sphere_backend.app import create_app
from sphere_backend.auth.dependencies import (
    get_auth_provider,
    get_jwks_client,
    get_sessionmaker,
)
from sphere_backend.auth.provider import AuthResult, TokenPair
from sphere_backend.db import Base, build_engine, build_sessionmaker


# ── doubles ──────────────────────────────────────────────────────────────────
class FakeProvider:
    jwks_url = "https://example.test/jwks"

    def __init__(self):
        self.exchange_result = AuthResult("wos_1", "researcher@stanford.edu", "acc_1", "ref_1")
        self.refresh_result = TokenPair("acc_2", "ref_2")
        self.last_state: str | None = None

    def authorization_url(self, *, state: str) -> str:
        self.last_state = state
        return f"https://workos.test/authorize?state={state}"

    def exchange_code(self, *, code: str) -> AuthResult:
        return self.exchange_result

    def refresh(self, *, refresh_token: str) -> TokenPair:
        return self.refresh_result

    def logout_url(self, *, session_id: str, return_to: str | None = None) -> str:
        self.last_logout = (session_id, return_to)
        return f"https://workos.test/logout?session_id={session_id}"


class _SigningKey:
    def __init__(self, key):
        self.key = key


class FakeJWKSClient:
    def __init__(self, public_key):
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token):  # mirrors PyJWKClient
        return _SigningKey(self._public_key)


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture
def keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, priv.public_key()


@pytest.fixture
def env(tmp_path, keypair):
    priv, pub = keypair
    db_path = tmp_path / "auth.db"
    # Create schema with a sync engine; the app uses an async engine on the same file.
    sync_engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = build_engine(f"sqlite+aiosqlite:///{db_path}")
    sessionmaker = build_sessionmaker(engine)
    provider = FakeProvider()

    app = create_app()
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    app.dependency_overrides[get_auth_provider] = lambda: provider
    app.dependency_overrides[get_jwks_client] = lambda: FakeJWKSClient(pub)

    client = TestClient(app)

    def mint(sub="wos_1", exp_delta=3600, key=None):
        now = int(time.time())
        return pyjwt.encode(
            {"sub": sub, "sid": "sess_1", "iss": "https://workos", "iat": now, "exp": now + exp_delta},
            key or priv,
            algorithm="RS256",
        )

    yield client, provider, mint


def _login_then(client, provider):
    """Drive /auth/login to set the state cookie; return the recorded state."""
    resp = client.get("/auth/login", follow_redirects=False)
    assert resp.status_code == 302
    return provider.last_state


# ── login ────────────────────────────────────────────────────────────────────
def test_login_redirects_to_workos_and_sets_state_cookie(env):
    client, provider, _ = env
    resp = client.get("/auth/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://workos.test/authorize?state=")
    assert _STATE_COOKIE in resp.cookies


# ── callback ─────────────────────────────────────────────────────────────────
def test_callback_provisions_user_and_returns_tokens(env):
    client, provider, _ = env
    state = _login_then(client, provider)
    resp = client.get(f"/auth/callback?code=abc&state={state}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == "acc_1"
    assert body["refresh_token"] == "ref_1"
    assert body["user"]["email"] == "researcher@stanford.edu"
    assert body["user"]["id"] >= 1


def test_callback_rejects_bad_state(env):
    client, provider, _ = env
    _login_then(client, provider)  # sets a real cookie
    resp = client.get("/auth/callback?code=abc&state=tampered")
    assert resp.status_code == 400


# ── logout ───────────────────────────────────────────────────────────────────
def test_logout_revokes_workos_session(env):
    client, provider, _ = env
    resp = client.get("/auth/logout?session_id=sess_1", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://workos.test/logout?session_id=sess_1"
    assert provider.last_logout[0] == "sess_1"


def test_logout_without_session_redirects_locally(env):
    client, *_ = env
    resp = client.get("/auth/logout", follow_redirects=False)
    assert resp.status_code == 302  # no session id → still log out client-side


def test_callback_redirects_with_token_fragment_when_portal_configured(tmp_path, keypair):
    # With portal_redirect_uri set, the callback hands tokens back to the SPA in
    # the URL fragment via a 302 (instead of returning JSON).
    from sphere_backend.config import Settings

    priv, pub = keypair
    db_path = tmp_path / "auth_redir.db"
    sync_engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    engine = build_engine(f"sqlite+aiosqlite:///{db_path}")
    sessionmaker = build_sessionmaker(engine)
    provider = FakeProvider()

    app = create_app(Settings(app_env="test", portal_redirect_uri="https://portal.test/app"))
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    app.dependency_overrides[get_auth_provider] = lambda: provider
    app.dependency_overrides[get_jwks_client] = lambda: FakeJWKSClient(pub)
    client = TestClient(app)

    state = _login_then(client, provider)
    resp = client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith("https://portal.test/app#")
    assert "access_token=acc_1" in loc
    assert "refresh_token=ref_1" in loc


# ── refresh (rotation) ───────────────────────────────────────────────────────
def test_refresh_returns_rotated_pair(env):
    client, provider, _ = env
    resp = client.post("/auth/refresh", json={"refresh_token": "ref_1"})
    assert resp.status_code == 200
    assert resp.json() == {"access_token": "acc_2", "refresh_token": "ref_2"}


# ── /auth/me + JWT verification ──────────────────────────────────────────────
def test_me_with_valid_token_returns_balance(env):
    client, provider, mint = env
    state = _login_then(client, provider)
    client.get(f"/auth/callback?code=abc&state={state}")  # provision wos_1 (+$10)
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {mint(sub='wos_1')}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "researcher@stanford.edu"
    assert body["credit_balance_usd"] == "10.000000"


def test_me_rejects_expired_token(env):
    client, provider, mint = env
    state = _login_then(client, provider)
    client.get(f"/auth/callback?code=abc&state={state}")
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {mint(exp_delta=-10)}"})
    assert resp.status_code == 401


def test_me_rejects_token_signed_by_other_key(env):
    client, provider, mint = env
    state = _login_then(client, provider)
    client.get(f"/auth/callback?code=abc&state={state}")
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {mint(key=other)}"})
    assert resp.status_code == 401


def test_me_requires_authorization_header(env):
    client, _, _ = env
    assert client.get("/auth/me").status_code == 401


def test_me_unknown_user_is_unauthorized(env):
    client, provider, mint = env
    # valid signature, but this sub was never provisioned
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {mint(sub='ghost')}"})
    assert resp.status_code == 401
