"""Reservation lifecycle for AI requests (BACKEND_DESIGN.md §4.4/§4.5).

Each AI request gets one ``api_usage_log`` row that doubles as its reservation
record. The hold on ``billing.reserved_usd`` is added at ``open_reservation`` and
removed exactly once — gated on the row's ``status`` transition under the billing
row lock, so settle / cancel / reclaim can't double-release:

    open_reservation  → status=pending,  reserved += RESERVE
    finalize          → status=settled,  reserved -= RESERVE, balance -= charge (+ ledger)
    cancel            → status=canceled, reserved -= RESERVE        (request failed)
    reclaim_stale     → cancel() any pending row older than a cutoff (crashed mid-stream)

All money math goes through the pure wallet core; this module only orchestrates
the DB transaction (lock → apply → persist → commit).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..billing import ModelRate, Usage, build_usage_record
from ..billing.usage import quantize_usd
from ..db.models import ApiUsageLog, Billing, CreditLedger
from ..wallet.core import InsufficientCreditsError, WalletState
from ..wallet.core import reserve as _core_reserve
from ..wallet.core import settle as _core_settle
from ..wallet.repository import WalletAccountNotFound

_ZERO = Decimal("0")


async def _lock_billing(session: AsyncSession, user_id: int) -> Billing:
    result = await session.execute(
        select(Billing).where(Billing.user_id == user_id).with_for_update()
    )
    billing = result.scalar_one_or_none()
    if billing is None:
        raise WalletAccountNotFound(user_id)
    return billing


async def _lock_row(session: AsyncSession, request_id: str) -> ApiUsageLog | None:
    result = await session.execute(
        select(ApiUsageLog).where(ApiUsageLog.request_id == request_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def open_reservation(
    session: AsyncSession,
    *,
    user_id: int,
    request_id: str,
    model: str,
    reserve_amount: Decimal,
    session_id: str | None = None,
    min_balance: Decimal = _ZERO,
) -> ApiUsageLog:
    """Place the hold and create the pending usage row atomically.

    Raises ``InsufficientCreditsError`` (→402) before any upstream call.
    """
    billing = await _lock_billing(session, user_id)
    state = WalletState(billing.credit_balance_usd, billing.reserved_usd)
    try:
        new_state = _core_reserve(state, reserve_amount, min_balance=min_balance)
    except InsufficientCreditsError:
        await session.rollback()
        raise
    billing.reserved_usd = new_state.reserved_usd
    row = ApiUsageLog(
        user_id=user_id,
        request_id=request_id,
        session_id=session_id,
        status="pending",
        model=model,
        reserved_usd=quantize_usd(Decimal(reserve_amount)),
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:  # duplicate request_id
        await session.rollback()
        raise
    return row


async def finalize(
    session: AsyncSession,
    *,
    request_id: str,
    usage: Usage,
    rate: ModelRate,
) -> ApiUsageLog | None:
    """Settle a pending reservation: release hold, charge actual, log usage.

    Idempotent — returns ``None`` if the row is missing or already finalized.
    """
    row = await _lock_row(session, request_id)
    if row is None or row.status != "pending":
        return None
    billing = await _lock_billing(session, row.user_id)
    record = build_usage_record(usage, rate)

    state = WalletState(billing.credit_balance_usd, billing.reserved_usd)
    new_state, entry = _core_settle(
        state, row.reserved_usd, record.user_charge_usd, type="ai_usage"
    )
    billing.credit_balance_usd = new_state.balance_usd
    billing.reserved_usd = new_state.reserved_usd
    session.add(
        CreditLedger(
            user_id=row.user_id,
            delta_usd=entry.delta_usd,
            balance_after=entry.balance_after,
            type="ai_usage",
            api_usage_id=row.id,
        )
    )

    row.status = "settled"
    row.finalized_at = datetime.now(timezone.utc)
    row.input_tokens = record.input_tokens
    row.cache_creation_tokens = record.cache_creation_tokens
    row.cache_read_tokens = record.cache_read_tokens
    row.output_tokens = record.output_tokens
    row.billed_input_tokens = record.billed_input_tokens
    row.billed_output_tokens = record.billed_output_tokens
    row.user_charge_usd = record.user_charge_usd
    row.sphere_cost_usd = record.sphere_cost_usd
    row.margin_usd = record.margin_usd
    await session.commit()
    return row


async def cancel(session: AsyncSession, *, request_id: str) -> ApiUsageLog | None:
    """Release a pending reservation's hold without charging. Idempotent."""
    row = await _lock_row(session, request_id)
    if row is None or row.status != "pending":
        return None
    billing = await _lock_billing(session, row.user_id)
    released = quantize_usd(billing.reserved_usd - row.reserved_usd)
    billing.reserved_usd = released if released > _ZERO else _ZERO
    row.status = "canceled"
    row.finalized_at = datetime.now(timezone.utc)
    await session.commit()
    return row


async def reclaim_stale(session: AsyncSession, *, older_than: datetime) -> list[str]:
    """Cancel pending reservations created before ``older_than`` (crashed requests).

    Returns the reclaimed ``request_id``s. Idempotent via ``cancel``'s status gate.
    """
    result = await session.execute(
        select(ApiUsageLog.request_id).where(
            ApiUsageLog.status == "pending", ApiUsageLog.created_at < older_than
        )
    )
    reclaimed: list[str] = []
    for request_id in result.scalars().all():
        if await cancel(session, request_id=request_id) is not None:
            reclaimed.append(request_id)
    return reclaimed
