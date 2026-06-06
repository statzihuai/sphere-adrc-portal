"""FastAPI application factory.

The browser portal calls this backend cross-origin (it's served from Stanford
AFS, the API from Railway/Render), so CORS is configured from ``Settings``.
Routers are mounted here; later slices add ``api/auth``, ``api/agent`` (the
Anthropic proxy), ``api/billing`` (Stripe), and ``api/data`` alongside health.

Run locally:  ``uvicorn sphere_backend.app:app --reload``
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .api import health
from .config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the app. Pass ``settings`` to override in tests."""
    settings = settings or get_settings()
    app = FastAPI(title="SPHERE Backend", version=__version__)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    return app


# Module-level instance for ``uvicorn sphere_backend.app:app``.
app = create_app()
