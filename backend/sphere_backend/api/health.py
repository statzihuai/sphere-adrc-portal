"""Liveness endpoint.

A dependency-free ``GET /health`` for load balancers, uptime checks, and the
deploy smoke test (BACKEND_DESIGN.md Milestone 0). It intentionally does *not*
touch Postgres/Redis/WorkOS/Stripe — it answers "is this process up?", not "are
its dependencies healthy?". A richer ``/readyz`` that checks downstreams can be
added when those wirings land.
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import __version__

router = APIRouter(tags=["meta"])


@router.get("/health")
def health() -> dict[str, str]:
    """Return process liveness. Always 200 when the app is serving."""
    return {"status": "ok", "service": "sphere-backend", "version": __version__}
