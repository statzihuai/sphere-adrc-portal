"""Pre-flight reserve estimation for the Anthropic proxy.

Because an SSE stream can't be un-sent, the wallet uses reserve→settle
(BACKEND_DESIGN.md §4.4): before forwarding a request we place a hold large
enough to cover the worst-case turn, then settle the actual charge afterward.

Open question 5 (design §8) is resolved here: the reserve is derived from the
request's ``max_output_tokens`` (worst case: the whole budget comes back as
output) plus a fixed fresh-input allowance, both at the platform rate. This
guarantees we never settle a turn we couldn't have pre-authorized, while not
over-blocking low-balance users more than the request's own ceiling implies.
"""

from __future__ import annotations

from decimal import Decimal

from .rates import ModelRate
from .usage import quantize_usd

# Fresh-input headroom folded into every reserve. Cache reads are far cheaper to
# the user only via the absent platform discount; this allowance covers the
# uncached prompt portion so the hold isn't dominated solely by output.
DEFAULT_INPUT_ALLOWANCE_TOKENS = 10_000


def reserve_estimate(
    rate: ModelRate,
    max_output_tokens: int,
    input_allowance_tokens: int = DEFAULT_INPUT_ALLOWANCE_TOKENS,
) -> Decimal:
    """Worst-case USD hold for one turn at this model and ``max_output_tokens``."""
    estimate = (
        Decimal(max_output_tokens) * rate.output_rate
        + Decimal(input_allowance_tokens) * rate.input_rate
    )
    return quantize_usd(estimate * rate.platform_mult)
