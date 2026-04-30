"""Tests for bearer-token authentication."""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app.main import _require_bearer, app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_protected_app() -> FastAPI:
    """Minimal app with one auth-guarded route for exercising _require_bearer."""
    test_app = FastAPI()

    @test_app.get("/protected", dependencies=[Depends(_require_bearer)])
    def protected() -> dict[str, bool]:
        return {"ok": True}

    return test_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def protected_client(monkeypatch: MonkeyPatch) -> TestClient:
    monkeypatch.setenv("GATEWAY_BEARER", "super-secret")
    return TestClient(_make_protected_app())


# ---------------------------------------------------------------------------
# Auth-protected route tests
# ---------------------------------------------------------------------------


def test_no_token_returns_401(protected_client: TestClient) -> None:
    assert protected_client.get("/protected").status_code == 401


def test_wrong_token_returns_401(protected_client: TestClient) -> None:
    response = protected_client.get("/protected", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


def test_correct_token_returns_200(protected_client: TestClient) -> None:
    response = protected_client.get("/protected", headers={"Authorization": "Bearer super-secret"})
    assert response.status_code == 200


def test_401_response_includes_www_authenticate_header(
    protected_client: TestClient,
) -> None:
    response = protected_client.get("/protected")
    assert response.headers.get("www-authenticate") == "Bearer"


# ---------------------------------------------------------------------------
# /health remains unauthed
# ---------------------------------------------------------------------------


def test_health_requires_no_token() -> None:
    """Verify /health is reachable without any Authorization header."""
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200


def test_health_not_blocked_by_wrong_token() -> None:
    """/health must pass even when an invalid token is presented."""
    with TestClient(app) as client:
        response = client.get("/health", headers={"Authorization": "Bearer totally-wrong"})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Documentation endpoints disabled
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_endpoints_disabled(path: str) -> None:
    """FastAPI's auto-generated docs must not leak alongside /health."""
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 404
