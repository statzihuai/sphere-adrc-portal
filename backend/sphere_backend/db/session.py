"""Async engine / session factory helpers.

Kept free of module-level globals so tests can build an isolated in-memory
engine. The FastAPI app wires a single engine + sessionmaker into ``app.state``
via its lifespan (added when the auth endpoints land) and exposes a
``get_session`` request dependency from there.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


def build_engine(database_url: str, **kwargs) -> AsyncEngine:
    """Create an async engine for ``database_url`` (e.g. ``postgresql+asyncpg://…``).

    Extra ``kwargs`` are passed through to ``create_async_engine`` (e.g. a
    ``StaticPool`` for an in-memory SQLite test engine).
    """
    kwargs.setdefault("future", True)
    kwargs.setdefault("pool_pre_ping", True)
    return create_async_engine(database_url, **kwargs)


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    """Session factory; ``expire_on_commit=False`` keeps ORM objects usable post-commit."""
    return async_sessionmaker(engine, expire_on_commit=False)
