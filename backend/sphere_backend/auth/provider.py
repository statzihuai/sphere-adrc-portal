"""WorkOS AuthKit adapter.

The endpoints depend on the ``AuthProvider`` Protocol — normalized dataclasses
in, normalized dataclasses out — so the rest of the app never touches WorkOS SDK
types and tests can substitute a fake provider with no WorkOS account or network.
SDK method names/signatures verified against the installed ``workos`` package:
``get_authorization_url(provider, redirect_uri, state)``,
``authenticate_with_code(code)`` / ``authenticate_with_refresh_token(refresh_token)``
→ object with ``.user`` (``.id``, ``.email``), ``.access_token``, ``.refresh_token``;
and ``get_jwks_url()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..config import Settings


@dataclass(frozen=True)
class AuthResult:
    """Normalized result of a code exchange."""

    workos_user_id: str
    email: str
    access_token: str
    refresh_token: str


@dataclass(frozen=True)
class TokenPair:
    """Rotated tokens from a refresh (WorkOS returns a new refresh token too)."""

    access_token: str
    refresh_token: str


@runtime_checkable
class AuthProvider(Protocol):
    @property
    def jwks_url(self) -> str: ...
    def authorization_url(self, *, state: str) -> str: ...
    def exchange_code(self, *, code: str) -> AuthResult: ...
    def refresh(self, *, refresh_token: str) -> TokenPair: ...


class WorkOSAuthProvider:
    """Production ``AuthProvider`` backed by the WorkOS Python SDK."""

    def __init__(self, client, *, redirect_uri: str):
        self._client = client
        self._redirect_uri = redirect_uri

    @property
    def jwks_url(self) -> str:
        return self._client.user_management.get_jwks_url()

    def authorization_url(self, *, state: str) -> str:
        return self._client.user_management.get_authorization_url(
            provider="authkit", redirect_uri=self._redirect_uri, state=state
        )

    def exchange_code(self, *, code: str) -> AuthResult:
        resp = self._client.user_management.authenticate_with_code(code=code)
        return AuthResult(
            workos_user_id=resp.user.id,
            email=resp.user.email,
            access_token=resp.access_token,
            refresh_token=resp.refresh_token,
        )

    def refresh(self, *, refresh_token: str) -> TokenPair:
        resp = self._client.user_management.authenticate_with_refresh_token(
            refresh_token=refresh_token
        )
        return TokenPair(access_token=resp.access_token, refresh_token=resp.refresh_token)


def build_workos_provider(settings: Settings) -> WorkOSAuthProvider | None:
    """Build the real provider, or ``None`` if WorkOS isn't configured."""
    if not (settings.workos_api_key and settings.workos_client_id):
        return None
    from workos import WorkOSClient

    client = WorkOSClient(
        api_key=settings.workos_api_key, client_id=settings.workos_client_id
    )
    return WorkOSAuthProvider(client, redirect_uri=settings.workos_redirect_uri)
