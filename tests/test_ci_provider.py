"""Tests for the ci-checks watch provider and change detectors."""

from __future__ import annotations

from aya.scheduler.providers import (
    _detect_ci_checks_complete,
    _detect_ci_checks_failed,
    _evaluate_auto_remove,
)
from aya.scheduler.types import CiChecksState, SchedulerItem


def _ci_state(
    *,
    passed: list[str] | None = None,
    failed: list[str] | None = None,
    pending: list[str] | None = None,
) -> CiChecksState:
    return CiChecksState(
        all_complete=not (pending or []),
        passed=passed or [],
        failed=failed or [],
        pending=pending or [],
    )


# ── change detectors ─────────────────────────────────────────────────────────


class TestDetectCiChecksFailed:
    def test_fails_detected(self):
        state = _ci_state(failed=["lint", "test"])
        assert _detect_ci_checks_failed(state, None) is True

    def test_all_pass_not_detected(self):
        state = _ci_state(passed=["lint", "test"])
        assert _detect_ci_checks_failed(state, None) is False

    def test_still_pending_not_detected(self):
        state = _ci_state(failed=["lint"], pending=["test"])
        assert _detect_ci_checks_failed(state, None) is False

    def test_empty_checks_not_detected(self):
        state = _ci_state()
        assert _detect_ci_checks_failed(state, None) is False


class TestDetectCiChecksComplete:
    def test_all_done(self):
        state = _ci_state(passed=["build"], failed=["lint"])
        assert _detect_ci_checks_complete(state, None) is True

    def test_still_pending(self):
        state = _ci_state(passed=["build"], pending=["lint"])
        assert _detect_ci_checks_complete(state, None) is False


# ── auto-remove ──────────────────────────────────────────────────────────────


class TestAutoRemoveCiChecks:
    def _item(self, **overrides) -> SchedulerItem:
        base: SchedulerItem = {
            "id": "test-id",
            "type": "watch",
            "status": "active",
            "created_at": "2026-01-01T00:00:00",
            "message": "CI checks",
            "tags": [],
            "session_required": False,
            "provider": "ci-checks",
            "remove_when": "checks_complete",
        }
        base.update(overrides)  # type: ignore[typeddict-item]
        return base

    def test_removes_when_complete(self):
        state = _ci_state(passed=["build"])
        assert _evaluate_auto_remove(self._item(), state) is True

    def test_keeps_when_pending(self):
        state = _ci_state(pending=["build"])
        assert _evaluate_auto_remove(self._item(), state) is False

    def test_no_remove_without_remove_when(self):
        state = _ci_state(passed=["build"])
        assert _evaluate_auto_remove(self._item(remove_when=""), state) is False
