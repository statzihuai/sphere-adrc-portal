"""Stripe billing endpoints (BACKEND_DESIGN.md §4.6).

`/billing/checkout/pack`       → Checkout session for a one-time credit pack
`/billing/checkout/subscribe`  → Checkout session for the $29/mo subscription
`/billing/portal`              → Stripe Customer Portal (manage card/sub)
`/billing/balance`             → current wallet balance
`/billing/webhook`             → verified Stripe events → wallet (the money-in path)
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import (
    current_user,
    get_app_settings,
    get_session,
    get_sessionmaker,
    get_stripe_client,
)
from ..billing.payments import ALLOWED_PACK_USD, ensure_customer, handle_event
from ..billing.stripe_client import StripeClient, WebhookVerificationError
from ..config import Settings
from ..db.models import User
from ..wallet import repository

router = APIRouter(prefix="/billing", tags=["billing"])


class PackRequest(BaseModel):
    amount_usd: Decimal


class CheckoutResponse(BaseModel):
    url: str


class BalanceResponse(BaseModel):
    balance_usd: str
    reserved_usd: str


@router.post("/checkout/pack", response_model=CheckoutResponse)
async def checkout_pack(
    body: PackRequest,
    user: User = Depends(current_user),
    stripe: StripeClient = Depends(get_stripe_client),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> CheckoutResponse:
    if body.amount_usd not in ALLOWED_PACK_USD:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "amount_usd must be one of 10, 25, 50")
    customer_id = await ensure_customer(session, stripe, user_id=user.id, email=user.email)
    url = stripe.create_payment_checkout(
        customer_id=customer_id,
        amount_cents=int(body.amount_usd * 100),
        success_url=settings.stripe_success_url,
        cancel_url=settings.stripe_cancel_url,
        metadata={"sphere_user_id": str(user.id), "credit_usd": str(body.amount_usd)},
    )
    return CheckoutResponse(url=url)


@router.post("/checkout/subscribe", response_model=CheckoutResponse)
async def checkout_subscribe(
    user: User = Depends(current_user),
    stripe: StripeClient = Depends(get_stripe_client),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> CheckoutResponse:
    if not settings.stripe_price_subscription:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "subscription price not configured")
    customer_id = await ensure_customer(session, stripe, user_id=user.id, email=user.email)
    url = stripe.create_subscription_checkout(
        customer_id=customer_id,
        price_id=settings.stripe_price_subscription,
        success_url=settings.stripe_success_url,
        cancel_url=settings.stripe_cancel_url,
    )
    return CheckoutResponse(url=url)


@router.post("/portal", response_model=CheckoutResponse)
async def portal(
    user: User = Depends(current_user),
    stripe: StripeClient = Depends(get_stripe_client),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> CheckoutResponse:
    customer_id = await ensure_customer(session, stripe, user_id=user.id, email=user.email)
    url = stripe.create_portal_session(
        customer_id=customer_id, return_url=settings.stripe_portal_return_url
    )
    return CheckoutResponse(url=url)


@router.get("/balance", response_model=BalanceResponse)
async def balance(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> BalanceResponse:
    state = await repository.get_state(session, user.id)
    return BalanceResponse(
        balance_usd=str(state.balance_usd), reserved_usd=str(state.reserved_usd)
    )


@router.post("/webhook")
async def webhook(
    request: Request,
    stripe: StripeClient = Depends(get_stripe_client),
    sessionmaker=Depends(get_sessionmaker),
    stripe_signature: str | None = Header(default=None),
) -> dict:
    payload = await request.body()
    try:
        event = stripe.verify_webhook(payload=payload, sig_header=stripe_signature or "")
    except WebhookVerificationError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid signature")
    async with sessionmaker() as session:
        handled = await handle_event(session, event)
    return {"status": handled}
