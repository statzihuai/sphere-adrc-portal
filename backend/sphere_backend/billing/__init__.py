"""Billing core: model rates, token charge/cost math, and reserve estimation.

All money is `Decimal` (never float) and quantized to 6 decimal places to match
the `NUMERIC(12,6)` columns in BACKEND_DESIGN.md §4.2. Token accounting must be
exact (brief §"Key constraints" #3) — these functions are the single source of
truth for what a user is charged and what SPHERE pays Anthropic.
"""

from .rates import ModelRate, UnknownModelError, get_rate, list_models
from .reserve import DEFAULT_INPUT_ALLOWANCE_TOKENS, reserve_estimate
from .usage import (
    Usage,
    UsageRecord,
    build_usage_record,
    margin,
    quantize_usd,
    sphere_cost,
    user_charge,
)

__all__ = [
    "ModelRate",
    "UnknownModelError",
    "get_rate",
    "list_models",
    "Usage",
    "UsageRecord",
    "build_usage_record",
    "margin",
    "quantize_usd",
    "sphere_cost",
    "user_charge",
    "reserve_estimate",
    "DEFAULT_INPUT_ALLOWANCE_TOKENS",
]
