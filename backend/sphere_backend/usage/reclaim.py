"""Background reclaim sweep for orphaned reservations (BACKEND_DESIGN.md §4.5).

A hold on ``billing.reserved_usd`` is normally released by ``finalize`` (settle)
or ``cancel`` (in the proxy's ``finally``). But on a hard client disconnect the
``finally``'s ``await cancel`` can be interrupted by task cancellation and not
complete, orphaning the hold. This sweep is the guarantee: it periodically
cancels any ``pending`` ``api_usage_log`` row older than the TTL, freeing the
hold.

The TTL must exceed the longest legitimate streamed turn — reclaiming a
still-running request would no-op its later ``finalize`` (status already
``canceled``) and the user would get that turn free. ``cancel`` is idempotent
under the row lock, so running several sweepers (multi-worker) is safe.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker

from .service import reclaim_stale

logger = logging.getLogger("sphere_backend.usage.reclaim")


async def reclaim_once(sessionmaker: async_sessionmaker, *, ttl_seconds: int) -> list[str]:
    """Run one sweep; return the ``request_id``s reclaimed."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
    async with sessionmaker() as session:
        reclaimed = await reclaim_stale(session, older_than=cutoff)
    if reclaimed:
        logger.info("reclaimed %d stale reservation(s): %s", len(reclaimed), reclaimed)
    return reclaimed


async def run_reclaim_loop(
    sessionmaker: async_sessionmaker, *, ttl_seconds: int, interval_seconds: int
) -> None:
    """Sweep forever every ``interval_seconds``. Cancel the task to stop it.

    Each iteration is isolated: a transient DB error is logged and the loop
    continues rather than dying (the sweep is a safety net, not a hot path).
    """
    logger.info(
        "reclaim loop started (ttl=%ss, interval=%ss)", ttl_seconds, interval_seconds
    )
    while True:
        try:
            await reclaim_once(sessionmaker, ttl_seconds=ttl_seconds)
        except asyncio.CancelledError:
            logger.info("reclaim loop stopping")
            raise
        except Exception:  # never let a transient failure kill the sweeper
            logger.exception("reclaim sweep failed; continuing")
        await asyncio.sleep(interval_seconds)
