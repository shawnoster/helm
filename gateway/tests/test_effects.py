"""Tests for /effects/kitt — Pydantic validation, auth, subprocess wiring."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app import effects
from app.main import app

BEARER = "test-bearer"
AUTH_HEADERS = {"Authorization": f"Bearer {BEARER}"}


# ---------------------------------------------------------------------------
# Fake Popen — records calls, simulates lifecycle without spawning anything
# ---------------------------------------------------------------------------


class FakePopen:
    """Drop-in replacement for subprocess.Popen used in route tests.

    Tracks every instance the route creates so tests can assert on the
    cmd args. Simulates a process that is "alive" until terminate() or
    kill() is called.
    """

    instances: list[FakePopen] = []

    def __init__(self, args: list[str], **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self._alive = True
        self.terminate_count = 0
        self.kill_count = 0
        FakePopen.instances.append(self)

    def poll(self) -> int | None:
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminate_count += 1
        self._alive = False

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        return 0

    def kill(self) -> None:
        self.kill_count += 1
        self._alive = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch: MonkeyPatch) -> Iterator[TestClient]:
    """TestClient with a known bearer, FakePopen stand-in, and clean module state."""
    monkeypatch.setenv("GATEWAY_BEARER", BEARER)
    monkeypatch.setattr(effects.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(effects, "_kitt_proc", None)
    FakePopen.instances.clear()
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 202 happy path — defaults, partial, full
# ---------------------------------------------------------------------------


def test_kitt_empty_body_uses_defaults(client: TestClient) -> None:
    response = client.post("/effects/kitt", headers=AUTH_HEADERS, json={})
    assert response.status_code == 202
    assert response.json() == {
        "started": True,
        "args": {"color": "red", "period": 1.8, "trail": 4},
    }


def test_kitt_no_body_at_all_uses_defaults(client: TestClient) -> None:
    """Empty request body (no JSON) should also work — Pydantic fills defaults."""
    response = client.post("/effects/kitt", headers=AUTH_HEADERS)
    assert response.status_code == 202
    assert response.json()["args"] == {"color": "red", "period": 1.8, "trail": 4}


def test_kitt_partial_body_fills_defaults(client: TestClient) -> None:
    response = client.post("/effects/kitt", headers=AUTH_HEADERS, json={"color": "blue"})
    assert response.status_code == 202
    assert response.json()["args"] == {"color": "blue", "period": 1.8, "trail": 4}


def test_kitt_full_body_passed_through(client: TestClient) -> None:
    body = {"color": "green", "period": 2.5, "trail": 6}
    response = client.post("/effects/kitt", headers=AUTH_HEADERS, json=body)
    assert response.status_code == 202
    assert response.json()["args"] == body


def test_kitt_spawns_subprocess_with_correct_args(client: TestClient) -> None:
    """Verify the exact CLI args handed to nanoleaf-kitt."""
    client.post(
        "/effects/kitt",
        headers=AUTH_HEADERS,
        json={"color": "amber", "period": 1.4, "trail": 5},
    )
    assert len(FakePopen.instances) == 1
    args = FakePopen.instances[0].args
    assert args[1:] == ["--color", "amber", "--period", "1.4", "--trail", "5"]
    assert args[0].endswith("nanoleaf-kitt")


# ---------------------------------------------------------------------------
# Validation — 400 on bad input (custom handler converts FastAPI's 422)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_body",
    [
        {"period": 0},
        {"period": -1.5},
        {"trail": 0},
        {"trail": -1},
        {"period": "fast"},
        {"trail": "long"},
    ],
)
def test_kitt_invalid_body_returns_400(client: TestClient, bad_body: dict[str, Any]) -> None:
    response = client.post("/effects/kitt", headers=AUTH_HEADERS, json=bad_body)
    assert response.status_code == 400
    assert "detail" in response.json()


def test_kitt_unparseable_json_returns_400(client: TestClient) -> None:
    response = client.post(
        "/effects/kitt",
        headers={**AUTH_HEADERS, "Content-Type": "application/json"},
        content=b"{not valid json",
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Auth — 401 without a valid bearer
# ---------------------------------------------------------------------------


def test_kitt_no_bearer_returns_401(client: TestClient) -> None:
    response = client.post("/effects/kitt", json={})
    assert response.status_code == 401
    assert FakePopen.instances == []


def test_kitt_wrong_bearer_returns_401(client: TestClient) -> None:
    response = client.post(
        "/effects/kitt",
        headers={"Authorization": "Bearer wrong"},
        json={},
    )
    assert response.status_code == 401
    assert FakePopen.instances == []


# ---------------------------------------------------------------------------
# Concurrency — kill the previous process before spawning a new one
# ---------------------------------------------------------------------------


def test_kitt_kills_previous_running_process(client: TestClient) -> None:
    client.post("/effects/kitt", headers=AUTH_HEADERS, json={"color": "red"})
    client.post("/effects/kitt", headers=AUTH_HEADERS, json={"color": "blue"})
    assert len(FakePopen.instances) == 2
    first, second = FakePopen.instances
    assert first.terminate_count == 1, "previous process must be terminated"
    assert second.terminate_count == 0, "new process is fresh, must not be touched"


def test_kitt_does_not_terminate_already_exited_process(client: TestClient) -> None:
    """If poll() returns non-None, the old process is gone — leave it alone."""
    client.post("/effects/kitt", headers=AUTH_HEADERS, json={})
    first = FakePopen.instances[0]
    first._alive = False  # simulate natural exit  # noqa: SLF001
    client.post("/effects/kitt", headers=AUTH_HEADERS, json={})
    assert first.terminate_count == 0, "exited process must not be re-terminated"
    assert len(FakePopen.instances) == 2


def test_kitt_tolerates_terminate_race(client: TestClient, monkeypatch: MonkeyPatch) -> None:
    """terminate() raising ProcessLookupError (process exited mid-request) must not 500."""

    def raising_terminate(self: FakePopen) -> None:
        raise ProcessLookupError("simulated race: process already exited")

    client.post("/effects/kitt", headers=AUTH_HEADERS, json={})
    monkeypatch.setattr(FakePopen, "terminate", raising_terminate)
    response = client.post("/effects/kitt", headers=AUTH_HEADERS, json={"color": "blue"})
    assert response.status_code == 202
    assert len(FakePopen.instances) == 2


def test_kitt_tolerates_kill_wait_timeout(client: TestClient, monkeypatch: MonkeyPatch) -> None:
    """kill+wait timing out must not 500 — give up gracefully and spawn new."""
    timeout = subprocess.TimeoutExpired(cmd="nanoleaf-kitt", timeout=2)

    def first_wait_times_out(self: FakePopen, timeout: float | None = None) -> int:  # noqa: ARG001
        raise subprocess.TimeoutExpired(cmd="nanoleaf-kitt", timeout=2)

    client.post("/effects/kitt", headers=AUTH_HEADERS, json={})
    monkeypatch.setattr(FakePopen, "wait", first_wait_times_out)
    response = client.post("/effects/kitt", headers=AUTH_HEADERS, json={"color": "amber"})
    assert response.status_code == 202
    assert len(FakePopen.instances) == 2
    # First process should have been kill()ed after the terminate+wait timeout
    assert FakePopen.instances[0].kill_count >= 1
    del timeout  # unused — kept for clarity
