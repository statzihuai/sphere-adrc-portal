"""Pure reserve→settle wallet logic (BACKEND_DESIGN.md §4.4).

Invariants enforced here:

* **Fail closed at zero.** ``reserve``/``deduct`` raise ``InsufficientCreditsError``
  (→ HTTP 402) when the *available* balance (balance − reserved) can't cover the
  amount, before any Anthropic call is forwarded.
* **An SSE stream can't be un-sent.** ``settle`` always succeeds: it releases the
  hold and deducts the actual charge even if that pushes the balance slightly
  negative on one unusually large turn. The next ``reserve`` then blocks at
  pre-flight, so the account can't keep spending.
* **Decimal-exact.** Every amount is quantized to 6 dp; no float ever touches a
  balance.

Each mutating call returns a new ``WalletState`` (the functions never mutate),
and the ones that move money also return the ``LedgerEntry`` to append to
``credit_ledger``. The DB adapter is responsible for doing the load → call →
persist under one ``FOR UPDATE`` transaction so concurrent requests for the same
user serialize — modeled in tests as sequential calls against the prior state.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from ..billing.usage import quantize_usd

_ZERO = Decimal("0")


class InsufficientCreditsError(Exception):
    """Available balance can't cover the requested hold/charge. → HTTP 402."""

    def __init__(self, requested: Decimal, available: Decimal):
        self.requested = requested
        self.available = available
        super().__init__(
            f"insufficient credits: requested {requested}, available {available}"
        )


@dataclass(frozen=True)
class WalletState:
    """A user's spendable balance and in-flight holds, in USD."""

    balance_usd: Decimal
    reserved_usd: Decimal = _ZERO

    @property
    def available_usd(self) -> Decimal:
        """Balance not already promised to an in-flight request."""
        return self.balance_usd - self.reserved_usd


@dataclass(frozen=True)
class LedgerEntry:
    """One row for ``credit_ledger`` (BACKEND_DESIGN.md §4.2).

    A reservation is a hold, not a ledger movement, so ``reserve`` produces no
    entry; ``settle`` / ``credit`` / ``deduct`` do.
    """

    delta_usd: Decimal       # positive = credit, negative = debit
    balance_after: Decimal
    type: str                # trial_grant | credit_pack | subscription_grant
    #                          | ai_usage | data_egress | refund
    description: str | None = None


def reserve(
    state: WalletState,
    amount: Decimal,
    min_balance: Decimal = _ZERO,
) -> WalletState:
    """Place a hold; raise ``InsufficientCreditsError`` if it can't be covered.

    No ledger entry — a reservation is a promise, settled later.
    """
    amount = quantize_usd(amount)
    if state.available_usd - amount < min_balance:
        raise InsufficientCreditsError(amount, state.available_usd)
    return replace(state, reserved_usd=quantize_usd(state.reserved_usd + amount))


def settle(
    state: WalletState,
    reserve_amount: Decimal,
    actual_charge: Decimal,
    *,
    type: str = "ai_usage",
    description: str | None = None,
) -> tuple[WalletState, LedgerEntry]:
    """Release a hold and deduct the actual charge. Always succeeds.

    May leave ``balance_usd`` marginally negative on a single oversized turn —
    accepted, because the stream is already sent; the next ``reserve`` blocks.
    """
    reserve_amount = quantize_usd(reserve_amount)
    actual_charge = quantize_usd(actual_charge)
    # Release the hold (never let reserved go negative due to rounding/mis-pair).
    new_reserved = quantize_usd(state.reserved_usd - reserve_amount)
    if new_reserved < _ZERO:
        new_reserved = _ZERO
    new_balance = quantize_usd(state.balance_usd - actual_charge)
    new_state = WalletState(balance_usd=new_balance, reserved_usd=new_reserved)
    entry = LedgerEntry(
        delta_usd=quantize_usd(-actual_charge),
        balance_after=new_balance,
        type=type,
        description=description,
    )
    return new_state, entry


def credit(
    state: WalletState,
    amount: Decimal,
    *,
    type: str,
    description: str | None = None,
) -> tuple[WalletState, LedgerEntry]:
    """Add funds (trial grant, credit pack, subscription grant, refund)."""
    amount = quantize_usd(amount)
    new_balance = quantize_usd(state.balance_usd + amount)
    new_state = replace(state, balance_usd=new_balance)
    entry = LedgerEntry(
        delta_usd=amount,
        balance_after=new_balance,
        type=type,
        description=description,
    )
    return new_state, entry


def deduct(
    state: WalletState,
    amount: Decimal,
    *,
    type: str,
    description: str | None = None,
    min_balance: Decimal | None = None,
) -> tuple[WalletState, LedgerEntry]:
    """Deduct a *known* cost up front (no reserve), e.g. a metered data pull.

    Pass ``min_balance`` to fail closed when the deduction would breach it; omit
    it for deductions that must always apply.
    """
    amount = quantize_usd(amount)
    if min_balance is not None and state.available_usd - amount < min_balance:
        raise InsufficientCreditsError(amount, state.available_usd)
    new_balance = quantize_usd(state.balance_usd - amount)
    new_state = replace(state, balance_usd=new_balance)
    entry = LedgerEntry(
        delta_usd=quantize_usd(-amount),
        balance_after=new_balance,
        type=type,
        description=description,
    )
    return new_state, entry
