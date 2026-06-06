"""Health endpoint tests.

Requires FastAPI + an HTTP test client (Starlette's ``TestClient`` uses httpx).
``importorskip`` keeps the pure-logic suite green in environments without the
server extras installed; install ``.[server,test]`` to run these.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from sphere_backend import __version__  # noqa: E402
from sphere_backend.app import create_app  # noqa: E402
from sphere_backend.config import Settings  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings(app_env="test")))


def test_health_returns_ok(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "service": "sphere-backend",
        "version": __version__,
    }


def test_app_factory_is_isolated():
    # Two apps from the factory are independent instances (safe for tests/workers).
    assert create_app() is not create_app()


def test_cors_preflight_allows_configured_origin():
    origin = "http://localhost:8000"
    client = TestClient(create_app(Settings(cors_origins=(origin,))))
    resp = client.options(
        "/health",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == origin


def test_unknown_route_is_404(client: TestClient):
    assert client.get("/does-not-exist").status_code == 404
