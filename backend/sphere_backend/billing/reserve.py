"""Pre-flight reserve estimation for the Anthropic proxy.

Because an SSE stream can't be un-sent, the wallet uses reserve→settle
(BACKEND_DESIGN.md §4.4): before forwarding a request we place a hold large
enough to cover the worst-case turn, then settle the actual charge afterward.

The hold must dominate the eventual ``user_charge``, whose input component is
``(input + cache_creation + cache_read)·input_rate`` — i.e. the user is billed
for **every** input token at full rate, cache status irrelevant (cache only
discounts SPHERE's cost, not the charge). So the reserve is driven by the total
number of input tokens the proxy is about to send, not a fixed guess:

    reserve = (input_tokens·input_rate + max_output_tokens·output_rate)·platform_mult

Since output is hard-capped at ``max_output_tokens`` and ``input_tokens`` is the
exact size of the outbound request, this hold is guaranteed ≥ the settled
charge. The proxy already holds the request body, so it can count/estimate the
input size and pass it in. ``fallback_input_tokens`` is used only when the count
is genuinely unknown — it is a floor for ignorance, not the normal path.
"""

from __future__ import annotations

from decimal import Decimal

from .rates import ModelRate
from .usage import quantize_usd

# Used only when the caller can't supply a real input-token count.
DEFAULT_FALLBACK_INPUT_TOKENS = 10_000


def reserve_estimate(
    rate: ModelRate,
    max_output_tokens: int,
    input_tokens: int | None = None,
    fallback_input_tokens: int = DEFAULT_FALLBACK_INPUT_TOKENS,
) -> Decimal:
    """Worst-case USD hold for one turn.

    Pass ``input_tokens`` = the total tokens being sent (system + tools +
    messages); the reserve then provably covers the settled charge. Omit it only
    when the count is unavailable, in which case ``fallback_input_tokens`` is
    used and the hold may under-cover a large-context turn.
    """
    if max_output_tokens < 0 or fallback_input_tokens < 0 or (
        input_tokens is not None and input_tokens < 0
    ):
        raise ValueError("token counts must be non-negative")
    billed_input = fallback_input_tokens if input_tokens is None else input_tokens
    estimate = (
        Decimal(billed_input) * rate.input_rate
        + Decimal(max_output_tokens) * rate.output_rate
    )
    return quantize_usd(estimate * rate.platform_mult)
