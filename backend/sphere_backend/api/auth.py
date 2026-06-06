"""WorkOS AuthKit endpoints (BACKEND_DESIGN.md §4.3).

`/auth/login`    → 302 to the AuthKit hosted page (CSRF `state` stored in a
                   short httpOnly cookie).
`/auth/callback` → verify `state`, exchange the code, provision the local user
                   (+ $10 trial on first login), return the tokens.
`/auth/refresh`  → exchange a refresh token for a rotated token pair.
`/auth/me`       → the authenticated user + credit balance.

The callback returns JSON tokens; how the static portal carries them back into
`sessionStorage` (popup postMessage vs. redirect-with-fragment) is a frontend
concern handled in the Phase-3 portal-integration slice.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import current_user, get_auth_provider, get_session
from ..auth.provider import AuthProvider
from ..config import get_settings
from ..db.models import Billing, User
from ..users import provision_user

router = APIRouter(prefix="/auth", tags=["auth"])

_STATE_COOKIE = "sphere_oauth_state"
_STATE_MAX_AGE = 600  # 10 minutes — matches the WorkOS code lifetime


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    user: dict


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str


class MeResponse(BaseModel):
    id: int
    email: str
    credit_balance_usd: str


@router.get("/login")
def login(provider: AuthProvider = Depends(get_auth_provider)) -> RedirectResponse:
    state = secrets.token_urlsafe(24)
    url = provider.authorization_url(state=state)
    redirect = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    redirect.set_cookie(
        _STATE_COOKIE,
        state,
        max_age=_STATE_MAX_AGE,
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="lax",
    )
    return redirect


@router.get("/callback", response_model=TokenResponse)
async def callback(
    code: str = Query(...),
    state: str = Query(...),
    sphere_oauth_state: str | None = Cookie(default=None),
    provider: AuthProvider = Depends(get_auth_provider),
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    if not sphere_oauth_state or not secrets.compare_digest(state, sphere_oauth_state):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or missing state")
    result = provider.exchange_code(code=code)
    user = await provision_user(
        session, workos_user_id=result.workos_user_id, email=result.email
    )
    return TokenResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        user={"id": user.id, "email": user.email},
    )


@router.post("/refresh", response_model=TokenPairResponse)
def refresh(
    body: RefreshRequest, provider: AuthProvider = Depends(get_auth_provider)
) -> TokenPairResponse:
    pair = provider.refresh(refresh_token=body.refresh_token)
    # Rotation: the caller must replace BOTH tokens with these.
    return TokenPairResponse(
        access_token=pair.access_token, refresh_token=pair.refresh_token
    )


@router.get("/me", response_model=MeResponse)
async def me(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> MeResponse:
    billing = await session.get(Billing, user.id)
    balance = str(billing.credit_balance_usd) if billing else "0"
    return MeResponse(id=user.id, email=user.email, credit_balance_usd=balance)
