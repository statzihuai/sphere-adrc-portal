"""Access-token (JWT) verification against WorkOS's JWKS.

WorkOS issues RS256 access tokens; we verify them ourselves against the JWKS at
``get_jwks_url()`` (design §4.3 step 5) — no per-request round-trip to WorkOS.
``PyJWKClient`` fetches and caches the signing keys. The verifier is split out so
tests can pass a fake JWKS client backed by a locally-generated keypair.
"""

from __future__ import annotations

import jwt
from jwt import PyJWKClient


class TokenError(Exception):
    """Access token missing, expired, or failed signature/claim validation → 401."""


def build_jwks_client(jwks_url: str) -> PyJWKClient:
    """JWKS client (caches keys; refreshes on a key-id miss)."""
    return PyJWKClient(jwks_url)


def verify_access_token(token: str, *, jwks_client) -> dict:
    """Return the verified claims, or raise ``TokenError``.

    ``jwks_client`` must expose ``get_signing_key_from_jwt(token).key`` (the real
    ``PyJWKClient`` does). Signature + ``exp`` are enforced; ``aud`` is not (WorkOS
    access tokens don't carry an audience by default).
    """
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_aud": False, "require": ["exp", "sub"]},
        )
    except Exception as exc:  # PyJWT raises a family of errors; collapse to one
        raise TokenError(str(exc)) from exc
    return claims
