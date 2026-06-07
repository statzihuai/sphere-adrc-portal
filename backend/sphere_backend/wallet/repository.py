"""DB-backed wallet: row-locked reserve/settle/credit/deduct (BACKEND_DESIGN.md §4.4).

This is the transactional layer over the pure ``wallet/core.py``. Each operation
runs as its own unit of work: ``SELECT … FOR UPDATE`` the user's billing row,
apply the pure function, persist the new balance/hold (+ ledger row), commit.
The row lock serializes concurrent operations for the same user, so two
simultaneous requests can't both pass the balance check and double-spend — the
no-double-spend invariant the pure layer can only model sequentially.

``credit``/``deduct`` accept an ``idempotency_key`` (unique in ``credit_ledger``)
so Stripe webhook retries and the like apply at most once.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..billing.usage import quantize_usd
from ..db.models import Billing, CreditLedger
from .core import (
    InsufficientCreditsError,
    LedgerEntry,
    WalletState,
)
from .core import credit as _credit
from .core import deduct as _deduct
from .core import reserve as _reserve
from .core import settle as _settle

_ZERO = Decimal("0")


class WalletAccountNotFound(Exception):
    """No billing row for this user (should always exist after provisioning)."""


async def _lock_billing(session: AsyncSession, user_id: int) -> Billing:
    result = await session.execute(
        select(Billing).where(Billing.user_id == user_id).with_for_update()
    )
    billing = result.scalar_one_or_none()
    if billing is None:
        raise WalletAccountNotFound(user_id)
    return billing


async def _already_applied(session: AsyncSession, idempotency_key: str | None) -> bool:
    if not idempotency_key:
        return False
    result = await session.execute(
        select(CreditLedger.id).where(CreditLedger.idempotency_key == idempotency_key)
    )
    return result.first() is not None


async def get_state(session: AsyncSession, user_id: int) -> WalletState:
    """Current balance/hold for a user (no lock)."""
    result = await session.execute(select(Billing).where(Billing.user_id == user_id))
    billing = result.scalar_one_or_none()
    if billing is None:
        raise WalletAccountNotFound(user_id)
    return WalletState(balance_usd=billing.credit_balance_usd, reserved_usd=billing.reserved_usd)


async def reserve(
    session: AsyncSession,
    user_id: int,
    amount: Decimal,
    *,
    min_balance: Decimal = _ZERO,
) -> Decimal:
    """Place a hold under a row lock; raise ``InsufficientCreditsError`` (→402) if short.

    Rolls back on rejection so the lock is released promptly for any waiter.
    Returns the quantized hold amount (to pass to ``settle`` later).
    """
    billing = await _lock_billing(session, user_id)
    state = WalletState(billing.credit_balance_usd, billing.reserved_usd)
    try:
        new_state = _reserve(state, amount, min_balance=min_balance)
    except InsufficientCreditsError:
        await session.rollback()
        raise
    billing.reserved_usd = new_state.reserved_usd
    await session.commit()
    return quantize_usd(Decimal(amount))


async def settle(
    session: AsyncSession,
    user_id: int,
    *,
    reserve_amount: Decimal,
    actual_charge: Decimal,
    type: str = "ai_usage",
    description: str | None = None,
    api_usage_id: int | None = None,
) -> LedgerEntry:
    """Release the hold and deduct the actual charge (+ ledger row). Always succeeds."""
    billing = await _lock_billing(session, user_id)
    state = WalletState(billing.credit_balance_usd, billing.reserved_usd)
    new_state, entry = _settle(
        state, reserve_amount, actual_charge, type=type, description=description
    )
    billing.credit_balance_usd = new_state.balance_usd
    billing.reserved_usd = new_state.reserved_usd
    session.add(
        CreditLedger(
            user_id=user_id,
            delta_usd=entry.delta_usd,
            balance_after=entry.balance_after,
            type=entry.type,
            description=entry.description,
            api_usage_id=api_usage_id,
        )
    )
    await session.commit()
    return entry


async def credit(
    session: AsyncSession,
    user_id: int,
    amount: Decimal,
    *,
    type: str,
    description: str | None = None,
    idempotency_key: str | None = None,
    stripe_pi_id: str | None = None,
) -> LedgerEntry | None:
    """Add funds (trial, credit pack, subscription, refund). Idempotent on ``idempotency_key``.

    Returns ``None`` if the key was already applied (no double-credit).
    """
    if await _already_applied(session, idempotency_key):
        return None
    billing = await _lock_billing(session, user_id)
    state = WalletState(billing.credit_balance_usd, billing.reserved_usd)
    new_state, entry = _credit(state, amount, type=type, description=description)
    billing.credit_balance_usd = new_state.balance_usd
    session.add(
        CreditLedger(
            user_id=user_id,
            delta_usd=entry.delta_usd,
            balance_after=entry.balance_after,
            type=entry.type,
            description=entry.description,
            idempotency_key=idempotency_key,
            stripe_pi_id=stripe_pi_id,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent insert with the same idempotency key won the race.
        await session.rollback()
        return None
    return entry


async def deduct(
    session: AsyncSession,
    user_id: int,
    amount: Decimal,
    *,
    type: str,
    description: str | None = None,
    min_balance: Decimal | None = None,
    idempotency_key: str | None = None,
) -> LedgerEntry | None:
    """Deduct a known cost up front (no reserve), e.g. metered egress. Idempotent."""
    if await _already_applied(session, idempotency_key):
        return None
    billing = await _lock_billing(session, user_id)
    state = WalletState(billing.credit_balance_usd, billing.reserved_usd)
    try:
        new_state, entry = _deduct(
            state, amount, type=type, description=description, min_balance=min_balance
        )
    except InsufficientCreditsError:
        await session.rollback()
        raise
    billing.credit_balance_usd = new_state.balance_usd
    session.add(
        CreditLedger(
            user_id=user_id,
            delta_usd=entry.delta_usd,
            balance_after=entry.balance_after,
            type=entry.type,
            description=entry.description,
            idempotency_key=idempotency_key,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return None
    return entry
