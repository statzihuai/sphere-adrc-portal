"""Billing-core tests.

The warm/cold cases are anchored to the worked examples in ENGINEER_BRIEF.md
§Phase 4 and BACKEND_DESIGN.md §4.5 (user $0.03000 / sphere $0.01785 warm;
$0.04350 / $0.04688 cold) computed at retail (platform_mult = 1.0). The seeded
Sonnet rate carries the locked 1.3× fee, so the seeded checks assert the warm
user charge is exactly 1.3× the retail figure and that sphere_cost is unaffected
by the platform fee.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from sphere_backend.billing import (
    Usage,
    UnknownModelError,
    build_usage_record,
    get_rate,
    margin,
    reserve_estimate,
    sphere_cost,
    user_charge,
)
from sphere_backend.billing.rates import per_token

# A retail (no-platform-fee) Sonnet rate to reproduce the brief's exact numbers.
RETAIL_SONNET = replace(get_rate("claude-sonnet-4-6"), platform_mult=Decimal("1"))

WARM = Usage(input_tokens=500, cache_creation_tokens=0, cache_read_tokens=4500, output_tokens=1000)
COLD = Usage(input_tokens=5000, cache_creation_tokens=4500, cache_read_tokens=0, output_tokens=1000)


# ── rate registry ────────────────────────────────────────────────────────────
def test_seeded_models_present():
    assert get_rate("claude-sonnet-4-6").input_rate == per_token("3")
    assert get_rate("claude-sonnet-4-6").output_rate == per_token("15")
    assert get_rate("claude-opus-4-8").input_rate == per_token("5")
    assert get_rate("claude-haiku-4-5").output_rate == per_token("5")


def test_default_platform_mult_is_1_3():
    assert get_rate("claude-sonnet-4-6").platform_mult == Decimal("1.3")


def test_unknown_model_fails_closed():
    with pytest.raises(UnknownModelError):
        get_rate("gpt-4o")


# ── brief-anchored retail examples (platform_mult = 1.0) ─────────────────────
def test_warm_retail_matches_brief():
    assert user_charge(WARM, RETAIL_SONNET) == Decimal("0.030000")
    assert sphere_cost(WARM, RETAIL_SONNET) == Decimal("0.017850")
    assert margin(WARM, RETAIL_SONNET) == Decimal("0.012150")  # ≈ 40.5%


def test_cold_retail_matches_brief_and_is_loss_making():
    assert user_charge(COLD, RETAIL_SONNET) == Decimal("0.043500")
    assert sphere_cost(COLD, RETAIL_SONNET) == Decimal("0.046875")
    assert margin(COLD, RETAIL_SONNET) == Decimal("-0.003375")  # ≈ -7.8%, expected loss


# ── seeded 1.3× platform fee ─────────────────────────────────────────────────
def test_platform_fee_scales_user_charge_not_sphere_cost():
    sonnet = get_rate("claude-sonnet-4-6")
    # user charge is exactly 1.3× the retail charge ...
    assert user_charge(WARM, sonnet) == Decimal("0.030000") * Decimal("1.3")
    assert user_charge(WARM, sonnet) == Decimal("0.039000")
    # ... while sphere cost is identical to retail (the fee is pure margin).
    assert sphere_cost(WARM, sonnet) == sphere_cost(WARM, RETAIL_SONNET)
    assert margin(WARM, sonnet) == Decimal("0.039000") - Decimal("0.017850")


def test_billed_input_sums_all_three_input_buckets():
    assert WARM.billed_input_tokens == 5000
    assert COLD.billed_input_tokens == 9500


def test_usage_record_fields():
    rec = build_usage_record(WARM, get_rate("claude-sonnet-4-6"))
    assert rec.model == "claude-sonnet-4-6"
    assert rec.billed_input_tokens == 5000
    assert rec.billed_output_tokens == 1000
    assert rec.user_charge_usd == Decimal("0.039000")
    assert rec.sphere_cost_usd == Decimal("0.017850")
    assert rec.margin_usd == rec.user_charge_usd - rec.sphere_cost_usd


def test_zero_usage_is_zero_charge():
    zero = Usage(0, 0, 0, 0)
    assert user_charge(zero, get_rate("claude-opus-4-8")) == Decimal("0.000000")
    assert sphere_cost(zero, get_rate("claude-opus-4-8")) == Decimal("0.000000")


def test_no_float_drift_everything_is_decimal():
    rec = build_usage_record(COLD, get_rate("claude-opus-4-8"))
    for value in (rec.user_charge_usd, rec.sphere_cost_usd, rec.margin_usd):
        assert isinstance(value, Decimal)
        # 6-dp storage precision, no binary-float tail.
        assert -value.as_tuple().exponent <= 6


# ── reserve estimation ───────────────────────────────────────────────────────
def test_reserve_estimate_fallback_when_input_unknown():
    # input unknown → 10k fallback: 8192·$15/1M + 10000·$3/1M = 0.15288, ×1.3
    est = reserve_estimate(get_rate("claude-sonnet-4-6"), max_output_tokens=8192)
    assert est == Decimal("0.198744")


def test_reserve_scales_with_max_output_and_model():
    opus = reserve_estimate(get_rate("claude-opus-4-8"), max_output_tokens=8192)
    sonnet = reserve_estimate(get_rate("claude-sonnet-4-6"), max_output_tokens=8192)
    assert opus > sonnet  # opus output is pricier → larger hold


def test_reserve_from_counted_input_covers_worst_case_charge():
    # The hold must dominate the settled charge. With input counted, it equals
    # the worst case (output hits max_tokens, every input token billed).
    rate = get_rate("claude-sonnet-4-6")
    input_tokens, max_output = 50_000, 8192
    est = reserve_estimate(rate, max_output, input_tokens=input_tokens)
    worst = user_charge(Usage(input_tokens, 0, 0, max_output), rate)
    assert est == worst
    # cache split is irrelevant — same billed_input → same charge, still covered
    cached = user_charge(Usage(10_000, 0, 40_000, max_output), rate)
    assert est >= cached


def test_fallback_under_reserves_large_context_so_proxy_must_count():
    # Documents the risk the count closes: a turn that reads a big cached prefix
    # is billed for all of it (cache_read at full input rate), so the fixed
    # fallback under-reserves while passing the real count covers it.
    rate = get_rate("claude-sonnet-4-6")
    actual = user_charge(Usage(0, 0, 50_000, 8192), rate)  # 50k read from cache
    assert reserve_estimate(rate, 8192) < actual                      # fallback: short
    assert reserve_estimate(rate, 8192, input_tokens=50_000) >= actual  # counted: covered


def test_reserve_estimate_rejects_negative_counts():
    rate = get_rate("claude-sonnet-4-6")
    with pytest.raises(ValueError):
        reserve_estimate(rate, -1)
    with pytest.raises(ValueError):
        reserve_estimate(rate, 8192, input_tokens=-5)
