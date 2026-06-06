"""Wallet reserve→settle tests.

Covers the edge cases in BACKEND_DESIGN.md §5: insufficient credits fails closed,
settle releases the hold and may go slightly negative on one oversized turn,
over-reservation is released cleanly, and sequential reserves model the per-user
``FOR UPDATE`` serialization (one winner, no double-spend).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sphere_backend.wallet import (
    InsufficientCreditsError,
    WalletState,
    credit,
    deduct,
    reserve,
    settle,
)

D = Decimal


def w(balance, reserved="0"):
    return WalletState(balance_usd=D(balance), reserved_usd=D(reserved))


# ── available balance ────────────────────────────────────────────────────────
def test_available_is_balance_minus_reserved():
    assert w("10.00", "3.00").available_usd == D("7.00")


# ── reserve ─────────────────────────────────────────────────────────────────
def test_reserve_holds_funds_without_moving_balance():
    s = reserve(w("10.00"), D("0.20"))
    assert s.reserved_usd == D("0.200000")
    assert s.balance_usd == D("10.00")
    assert s.available_usd == D("9.800000")


def test_reserve_insufficient_raises_402_and_does_not_mutate():
    start = w("0.10")
    with pytest.raises(InsufficientCreditsError) as exc:
        reserve(start, D("0.20"))
    assert exc.value.requested == D("0.200000")
    assert exc.value.available == D("0.10")
    # caller still holds the unchanged state (functions never mutate)
    assert start.balance_usd == D("0.10")
    assert start.reserved_usd == D("0")


def test_reserve_respects_min_balance_floor():
    # floor of $0.50: a hold that drops available below it is rejected
    with pytest.raises(InsufficientCreditsError):
        reserve(w("1.00"), D("0.60"), min_balance=D("0.50"))
    # exactly to the floor is allowed
    s = reserve(w("1.00"), D("0.50"), min_balance=D("0.50"))
    assert s.available_usd == D("0.500000")


# ── settle ──────────────────────────────────────────────────────────────────
def test_settle_releases_hold_and_deducts_actual():
    held = reserve(w("10.00"), D("0.20"))            # reserved 0.20
    after, entry = settle(held, D("0.20"), D("0.03"))
    assert after.reserved_usd == D("0")              # hold released
    assert after.balance_usd == D("9.970000")        # only actual deducted
    assert entry.delta_usd == D("-0.030000")
    assert entry.balance_after == D("9.970000")
    assert entry.type == "ai_usage"


def test_settle_releases_full_reserve_even_if_actual_smaller():
    held = reserve(w("5.00"), D("1.00"))             # over-reserved
    after, _ = settle(held, D("1.00"), D("0.10"))
    assert after.reserved_usd == D("0")              # whole hold released
    assert after.balance_usd == D("4.900000")


def test_settle_can_go_slightly_negative_then_blocks_next_reserve():
    # one oversized turn: balance 0.05, actual charge 0.12 (stream already sent)
    held = reserve(w("0.05"), D("0.05"))
    after, entry = settle(held, D("0.05"), D("0.12"))
    assert after.balance_usd == D("-0.070000")       # allowed once
    assert entry.balance_after == D("-0.070000")
    # fail closed: the next reserve at a negative balance is rejected
    with pytest.raises(InsufficientCreditsError):
        reserve(after, D("0.01"))


def test_settle_never_leaves_negative_reserved():
    # defensive: settling a larger reserve than is held clamps reserved at 0
    after, _ = settle(w("1.00", "0.05"), D("0.20"), D("0.02"))
    assert after.reserved_usd == D("0")


# ── concurrency: sequential reserves model FOR UPDATE serialization ──────────
def test_two_competing_reserves_only_one_wins():
    # balance 0.30; two turns each need a 0.20 hold — only the first fits
    start = w("0.30")
    first = reserve(start, D("0.20"))                # winner
    assert first.available_usd == D("0.100000")
    with pytest.raises(InsufficientCreditsError):
        reserve(first, D("0.20"))                    # loser, no double-spend


# ── credit / deduct ─────────────────────────────────────────────────────────
def test_credit_adds_funds_with_ledger_entry():
    after, entry = credit(w("0.00"), D("10.00"), type="trial_grant")
    assert after.balance_usd == D("10.000000")
    assert entry.delta_usd == D("10.000000")
    assert entry.balance_after == D("10.000000")
    assert entry.type == "trial_grant"


def test_deduct_known_cost_with_floor_fails_closed():
    with pytest.raises(InsufficientCreditsError):
        deduct(w("0.05"), D("0.10"), type="data_egress", min_balance=D("0"))


def test_deduct_without_floor_always_applies():
    after, entry = deduct(w("0.05"), D("0.10"), type="data_egress")
    assert after.balance_usd == D("-0.050000")
    assert entry.type == "data_egress"


def test_amounts_are_decimal_quantized_to_six_dp():
    after, entry = credit(w("0"), D("1"), type="credit_pack")
    assert isinstance(after.balance_usd, Decimal)
    assert -entry.balance_after.as_tuple().exponent <= 6
