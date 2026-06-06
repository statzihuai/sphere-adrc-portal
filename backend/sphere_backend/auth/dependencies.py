"""FastAPI request dependencies for the DB session, WorkOS provider, and the
authenticated current user.

Engine/sessionmaker, the WorkOS provider, and the JWKS client are built once at
startup into ``app.state`` (see ``app.lifespan``). These accessors pull them from
there, so tests can override the accessors with doubles via
``app.dependency_overrides`` and never need real infrastructure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import User
from .jwt import TokenError, verify_access_token


def get_sessionmaker(request: Request):
    sm = getattr(request.app.state, "sessionmaker", None)
    if sm is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database not configured")
    return sm


async def get_session(sm=Depends(get_sessionmaker)) -> AsyncIterator[AsyncSession]:
    async with sm() as session:
        yield session


def get_auth_provider(request: Request):
    provider = getattr(request.app.state, "auth_provider", None)
    if provider is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "WorkOS not configured")
    return provider


def get_jwks_client(request: Request):
    client = getattr(request.app.state, "jwks_client", None)
    if client is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "WorkOS not configured")
    return client


async def current_user(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    jwks_client=Depends(get_jwks_client),
) -> User:
    """Resolve the local user from a verified ``Authorization: Bearer`` token."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization[len("Bearer ") :].strip()
    try:
        claims = verify_access_token(token, jwks_client=jwks_client)
    except TokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")

    result = await session.execute(
        select(User).where(User.workos_user_id == claims["sub"])
    )
    user = result.scalar_one_or_none()
    if user is None:
        # Valid token but no local account — shouldn't happen post-signup.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not provisioned")
    return user
