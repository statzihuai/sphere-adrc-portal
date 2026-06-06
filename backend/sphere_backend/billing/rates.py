"""Model pricing registry.

Mirrors the ``model_rates`` table in BACKEND_DESIGN.md §4.2. In production these
rows live in Postgres and are editable without a redeploy; this in-memory seed is
the default/bootstrap and the single place rates are defined for the pure-logic
layer. Anthropic changes pricing — never hardcode rates at a call site; resolve
through ``get_rate``.

Decision (design §8): provider is Anthropic. Default served model is Sonnet 4.6;
Opus 4.8 is the premium tier; Haiku 4.5 is for cheap lookups. ``platform_mult``
is the transparent 1.3× fee. ``sphere_*`` rates default to retail (no assumed
volume discount — conservative); set a negotiated discount later if one exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# Transparent platform fee applied to the user-facing charge (design §8, locked).
PLATFORM_MULT = Decimal("1.3")

# Cache multipliers applied to SPHERE's cost (Anthropic prompt caching).
# 1.25× write is the 5-minute-TTL rate (2.0× for 1-hour TTL); 0.10× read.
CACHE_WRITE_MULT_5M = Decimal("1.25")
CACHE_READ_MULT = Decimal("0.10")


class UnknownModelError(Exception):
    """A model has no priced entry. Fail closed at the proxy edge → HTTP 400.

    Never serve an unpriced model — doing so would mean we can't compute the
    charge and would undercharge the user (design §5).
    """


@dataclass(frozen=True)
class ModelRate:
    """Per-token rates for one model. All values are $/token as ``Decimal``."""

    model: str
    input_rate: Decimal          # user-facing retail $/token (fresh input)
    output_rate: Decimal         # user-facing retail $/token (output)
    platform_mult: Decimal       # transparent platform fee, applied to user charge
    sphere_input_rate: Decimal   # SPHERE's $/token to Anthropic (input)
    sphere_output_rate: Decimal  # SPHERE's $/token to Anthropic (output)
    cache_write_mult: Decimal = CACHE_WRITE_MULT_5M
    cache_read_mult: Decimal = CACHE_READ_MULT


def per_token(per_million: str | Decimal) -> Decimal:
    """Convert a $/1M-tokens figure to an exact $/token ``Decimal``."""
    return Decimal(per_million) / Decimal(1_000_000)


_RATES: dict[str, ModelRate] = {}


def _seed(model: str, input_per_million: str, output_per_million: str) -> None:
    rate = ModelRate(
        model=model,
        input_rate=per_token(input_per_million),
        output_rate=per_token(output_per_million),
        platform_mult=PLATFORM_MULT,
        # sphere_* default to retail — no volume discount assumed (conservative).
        sphere_input_rate=per_token(input_per_million),
        sphere_output_rate=per_token(output_per_million),
    )
    _RATES[model] = rate


# Authoritative Anthropic pricing as of 2026-06 (BACKEND_DESIGN.md §4.2 seed).
_seed("claude-sonnet-4-6", "3", "15")   # default served model
_seed("claude-opus-4-8", "5", "25")     # premium tier
_seed("claude-haiku-4-5", "1", "5")     # cheap lookups


def get_rate(model: str) -> ModelRate:
    """Return the priced ``ModelRate`` for ``model`` or raise ``UnknownModelError``."""
    try:
        return _RATES[model]
    except KeyError as exc:
        raise UnknownModelError(model) from exc


def list_models() -> list[str]:
    """All priced model ids (default-first ordering is not guaranteed)."""
    return list(_RATES)
