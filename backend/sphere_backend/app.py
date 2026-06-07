"""FastAPI application factory.

The browser portal calls this backend cross-origin (it's served from Stanford
AFS, the API from Railway/Render), so CORS is configured from ``Settings``.
Routers are mounted here; later slices add ``api/agent`` (the Anthropic proxy),
``api/billing`` (Stripe), and ``api/data`` alongside health and auth.

The lifespan builds shared infrastructure once into ``app.state``: the async DB
engine + sessionmaker, the WorkOS provider, and the JWKS client. If WorkOS isn't
configured, the provider/JWKS client are ``None`` and the auth endpoints return
503 (the rest of the app still works).

Run locally:  ``uvicorn sphere_backend.app:app --reload``
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .api import agent, auth, billing, health
from .auth.provider import build_workos_provider
from .auth.jwt import build_jwks_client
from .billing.stripe_client import build_stripe_client
from .config import Settings, get_settings
from .db import Base
from .db.session import build_engine, build_sessionmaker
from .proxy.upstream import build_anthropic_streamer
from .usage import run_reclaim_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = getattr(app.state, "settings", None) or get_settings()
    engine = build_engine(settings.database_url)
    app.state.engine = engine
    app.state.sessionmaker = build_sessionmaker(engine)

    # Dev convenience: auto-create the SQLite schema so local runs are turnkey.
    # Postgres owns its schema via Alembic migrations — never auto-created.
    if engine.dialect.name == "sqlite":
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    provider = build_workos_provider(settings)
    app.state.auth_provider = provider
    app.state.jwks_client = build_jwks_client(provider.jwks_url) if provider else None

    app.state.anthropic_streamer = (
        build_anthropic_streamer(settings.anthropic_api_key, settings.anthropic_base_url)
        if settings.anthropic_api_key
        else None
    )
    app.state.stripe_client = build_stripe_client(settings)

    # Background reclaim sweep — the guarantee that orphaned holds are freed.
    reclaim_task = asyncio.create_task(
        run_reclaim_loop(
            app.state.sessionmaker,
            ttl_seconds=settings.reservation_ttl_seconds,
            interval_seconds=settings.reclaim_interval_seconds,
        )
    )

    try:
        yield
    finally:
        reclaim_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reclaim_task
        await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the app. Pass ``settings`` to override in tests."""
    settings = settings or get_settings()
    app = FastAPI(title="SPHERE Backend", version=__version__, lifespan=lifespan)
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(agent.router)
    app.include_router(billing.router)
    return app


# Module-level instance for ``uvicorn sphere_backend.app:app``.
app = create_app()
