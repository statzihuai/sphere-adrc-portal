"""Prepaid wallet: pure reserve→settle state transitions.

This module is the integrity core of BACKEND_DESIGN.md §4.4. It contains *only*
the money arithmetic and the 402 decision as pure functions over an immutable
``WalletState`` — no database. The Postgres adapter (a thin ``SELECT ... FOR
UPDATE`` transaction that loads a row, calls these functions, and writes the
result + ledger entry) is a later slice; keeping the decision logic pure makes
it exhaustively testable without a DB and keeps the serialization concern in one
place.
"""

from .core import (
    InsufficientCreditsError,
    LedgerEntry,
    WalletState,
    credit,
    deduct,
    reserve,
    settle,
)

__all__ = [
    "InsufficientCreditsError",
    "LedgerEntry",
    "WalletState",
    "credit",
    "deduct",
    "reserve",
    "settle",
]
