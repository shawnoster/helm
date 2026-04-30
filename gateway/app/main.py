"""aya-gateway HTTP service."""

import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer_scheme = HTTPBearer(auto_error=False)


def _version() -> str:
    """Resolve the build version from the env at call time.

    Reading per-call (rather than caching at import) keeps the contract
    testable via monkeypatch without importlib reloads. In production
    GIT_SHA is set once at container start and doesn't change, so the
    cost is a single env lookup per /health request — negligible.
    """
    return os.getenv("GIT_SHA", "dev")


def _bearer_token() -> str:
    """Return GATEWAY_BEARER from the environment.

    Read per-call (not cached) so tests can override it with monkeypatch
    without reloading the module. In production the value is fixed at
    container start and never changes.
    """
    return os.getenv("GATEWAY_BEARER", "").strip()


def _require_bearer(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),  # noqa: B008
) -> None:
    """FastAPI dependency — reject requests that lack a valid bearer token."""
    expected = _bearer_token()
    if (
        credentials is None
        or not expected
        or not secrets.compare_digest(credentials.credentials, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Fail fast at startup if GATEWAY_BEARER is absent."""
    if not _bearer_token():
        raise RuntimeError("GATEWAY_BEARER is not set — refusing to start without a bearer token")
    yield


app = FastAPI(
    title="aya-gateway",
    version=_version(),
    lifespan=_lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Router for all authenticated endpoints. Add future routes here.
authenticated = APIRouter(dependencies=[Depends(_require_bearer)])
app.include_router(authenticated)


@app.get("/health")
def health() -> dict[str, bool | str]:
    return {"ok": True, "version": _version()}
