"""Token usage accounting and the charge/cost/margin math.

These functions implement the billing formula in BACKEND_DESIGN.md §4.5:

    billed_input = input + cache_creation + cache_read
    user_charge  = (billed_input·input_rate + output·output_rate) · platform_mult
    sphere_cost  = input·s_input
                 + cache_creation·s_input·cache_write_mult
                 + cache_read·s_input·cache_read_mult
                 + output·s_output
    margin       = user_charge - sphere_cost

The user is billed as if no caching existed (every input token at full rate);
the cache spread shows up only in ``sphere_cost``, as upside rather than the
foundation of the margin (the 1.3× platform fee is the foundation).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from .rates import ModelRate

# Quantize to 6 decimal places — matches NUMERIC(12,6) in the schema.
_USD_QUANTUM = Decimal("0.000001")


def quantize_usd(amount: Decimal) -> Decimal:
    """Round a USD ``Decimal`` to 6 dp (the storage precision)."""
    return amount.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Usage:
    """The four token counts Anthropic returns for a request.

    ``input_tokens`` + ``cache_*`` come from the ``message_start`` event;
    ``output_tokens`` from the final ``message_delta`` (design §4.5). Missing
    ``cache_read_tokens`` would undercharge the user, so all four are required.
    """

    input_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    output_tokens: int

    @property
    def billed_input_tokens(self) -> int:
        """All input billed to the user at full rate (caching invisible to them)."""
        return (
            self.input_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )


def user_charge(usage: Usage, rate: ModelRate) -> Decimal:
    """What the user pays: retail on every input+output token, times platform fee."""
    base = (
        Decimal(usage.billed_input_tokens) * rate.input_rate
        + Decimal(usage.output_tokens) * rate.output_rate
    )
    return quantize_usd(base * rate.platform_mult)


def sphere_cost(usage: Usage, rate: ModelRate) -> Decimal:
    """What SPHERE actually pays Anthropic (cache writes 1.25×, reads 0.10×)."""
    cost = (
        Decimal(usage.input_tokens) * rate.sphere_input_rate
        + Decimal(usage.cache_creation_tokens)
        * rate.sphere_input_rate
        * rate.cache_write_mult
        + Decimal(usage.cache_read_tokens)
        * rate.sphere_input_rate
        * rate.cache_read_mult
        + Decimal(usage.output_tokens) * rate.sphere_output_rate
    )
    return quantize_usd(cost)


def margin(usage: Usage, rate: ModelRate) -> Decimal:
    """``user_charge - sphere_cost`` for one request."""
    return quantize_usd(user_charge(usage, rate) - sphere_cost(usage, rate))


@dataclass(frozen=True)
class UsageRecord:
    """One row for ``api_usage_log`` (BACKEND_DESIGN.md §4.2)."""

    model: str
    input_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    output_tokens: int
    billed_input_tokens: int
    billed_output_tokens: int
    user_charge_usd: Decimal
    sphere_cost_usd: Decimal
    margin_usd: Decimal


def build_usage_record(usage: Usage, rate: ModelRate) -> UsageRecord:
    """Compute every logged field for one request in a single pass."""
    charge = user_charge(usage, rate)
    cost = sphere_cost(usage, rate)
    return UsageRecord(
        model=rate.model,
        input_tokens=usage.input_tokens,
        cache_creation_tokens=usage.cache_creation_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        output_tokens=usage.output_tokens,
        billed_input_tokens=usage.billed_input_tokens,
        billed_output_tokens=usage.output_tokens,
        user_charge_usd=charge,
        sphere_cost_usd=cost,
        margin_usd=quantize_usd(charge - cost),
    )
