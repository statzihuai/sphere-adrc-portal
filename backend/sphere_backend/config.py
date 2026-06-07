"""Runtime configuration, read from the environment.

Deliberately dependency-light (stdlib + a frozen dataclass) so the scaffold has
no new runtime requirements beyond FastAPI itself. Later slices that need typed,
validated settings (DB URL, WorkOS/Stripe keys) can swap this for
``pydantic-settings`` without changing call sites — everything goes through
``get_settings()``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

# Default CORS origins: the live Stanford AFS portal plus local dev. Override
# with a comma-separated SPHERE_CORS_ORIGINS env var in other environments.
_DEFAULT_CORS_ORIGINS = (
    "https://web.stanford.edu",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
)


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    """Process configuration. Construct via ``get_settings()`` (cached)."""

    app_env: str = "development"          # development | staging | production
    cors_origins: tuple[str, ...] = field(default_factory=lambda: _DEFAULT_CORS_ORIGINS)
    # Async SQLAlchemy URL. SQLite default for local dev; Postgres in prod
    # (postgresql+asyncpg://…). Override with SPHERE_DATABASE_URL.
    database_url: str = "sqlite+aiosqlite:///./sphere.db"
    # WorkOS AuthKit. Empty until configured → auth endpoints return 503.
    workos_api_key: str = ""
    workos_client_id: str = ""
    workos_redirect_uri: str = "http://localhost:8000/auth/callback"
    # Anthropic — SPHERE's centralized key (the margin engine). Empty → /v1/agent 503.
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    default_model: str = "claude-sonnet-4-6"

    @property
    def cookie_secure(self) -> bool:
        """Mark cookies Secure outside local dev (so they work over http in tests)."""
        return self.app_env != "development"


@lru_cache
def get_settings() -> Settings:
    """Return the cached process settings, materialized from the environment."""
    return Settings(
        app_env=os.environ.get("SPHERE_APP_ENV", "development"),
        cors_origins=_csv_env("SPHERE_CORS_ORIGINS", _DEFAULT_CORS_ORIGINS),
        database_url=os.environ.get(
            "SPHERE_DATABASE_URL", "sqlite+aiosqlite:///./sphere.db"
        ),
        workos_api_key=os.environ.get("WORKOS_API_KEY", ""),
        workos_client_id=os.environ.get("WORKOS_CLIENT_ID", ""),
        workos_redirect_uri=os.environ.get(
            "WORKOS_REDIRECT_URI", "http://localhost:8000/auth/callback"
        ),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        anthropic_base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        default_model=os.environ.get("SPHERE_DEFAULT_MODEL", "claude-sonnet-4-6"),
    )
