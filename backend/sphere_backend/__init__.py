"""SPHERE backend — auth, wallet, Anthropic proxy, billing.

See ../../BACKEND_DESIGN.md for the full design. This package currently contains
the pure-logic correctness core (billing math + wallet reserve/settle); the
FastAPI app, DB adapters, WorkOS auth, and Stripe wiring are added in later
slices on top of these primitives.
"""
