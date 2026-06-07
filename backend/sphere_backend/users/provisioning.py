"""Local user provisioning + signup trial grant (BACKEND_DESIGN.md §4.3 step 3).

This is *our* provisioning — creating the local SPHERE user/billing rows the
first time someone authenticates — distinct from WorkOS's SSO "JIT provisioning"
feature. It's idempotent on ``workos_user_id``: a repeat call (same user signing
in again, a retry, or a concurrent first-login) returns the existing user without
re-granting the trial.

The $10 trial is computed through the pure wallet ``credit()`` so the balance and
ledger entry stay consistent with every other money movement, and is tagged with
a unique ``idempotency_key`` so the grant can land at most once.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Billing, CreditLedger, User
from ..wallet import WalletState, credit

TRIAL_GRANT_USD = Decimal("10")


async def _get_by_workos_id(session: AsyncSession, workos_user_id: str) -> User | None:
    result = await session.execute(
        select(User).where(User.workos_user_id == workos_user_id)
    )
    return result.scalar_one_or_none()


async def provision_user(
    session: AsyncSession,
    *,
    workos_user_id: str,
    email: str,
    stripe_customer_id: str | None = None,
    trial_amount: Decimal = TRIAL_GRANT_USD,
) -> User:
    """Return the local user for ``workos_user_id``, creating it (+ trial) if new.

    Idempotent: existing users are returned untouched; a lost race on the unique
    ``workos_user_id`` resolves to the winner's row (which already got the trial).
    """
    existing = await _get_by_workos_id(session, workos_user_id)
    if existing is not None:
        return existing

    user = User(workos_user_id=workos_user_id, email=email)
    session.add(user)
    try:
        await session.flush()  # assign user.id; first point a race can collide

        # Grant the trial through the pure wallet so balance + ledger agree.
        new_state, entry = credit(
            WalletState(balance_usd=Decimal("0")),
            trial_amount,
            type="trial_grant",
            description="signup trial credit",
        )
        session.add(
            Billing(
                user_id=user.id,
                stripe_customer_id=stripe_customer_id,
                credit_balance_usd=new_state.balance_usd,
                reserved_usd=new_state.reserved_usd,
                trial_used=True,
            )
        )
        session.add(
            CreditLedger(
                user_id=user.id,
                delta_usd=entry.delta_usd,
                balance_after=entry.balance_after,
                type=entry.type,
                description=entry.description,
                idempotency_key=f"trial:{user.id}",
            )
        )
        await session.commit()
    except IntegrityError:
        # Concurrent first-login won the insert; return its fully-provisioned row.
        await session.rollback()
        winner = await _get_by_workos_id(session, workos_user_id)
        if winner is None:  # pragma: no cover - constraint violation with no winner
            raise
        return winner

    await session.refresh(user)
    return user
