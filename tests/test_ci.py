"""Tests for the ci module — PR check watching logic."""

from __future__ import annotations

import json
from unittest.mock import patch

from aya.ci import _poll_checks, watch_pr_checks


def _noop_sleep(n: float) -> None:
    pass


# ── _poll_checks ──────────────────────────────────────────────────────────────


class TestPollChecks:
    def test_all_pass(self):
        checks = json.dumps([{"name": "build", "state": "completed", "conclusion": "success"}])
        with patch("aya.ci._run", return_value=(0, checks)):
            status, failed = _poll_checks("42", sleep_fn=_noop_sleep)
        assert status == "pass"
        assert failed == []

    def test_some_fail(self):
        checks = json.dumps(
            [
                {"name": "build", "state": "completed", "conclusion": "success"},
                {"name": "lint", "state": "completed", "conclusion": "failure"},
            ]
        )
        with patch("aya.ci._run", return_value=(0, checks)):
            status, failed = _poll_checks("42", sleep_fn=_noop_sleep)
        assert status == "fail"
        assert failed == ["lint"]

    def test_timeout(self):
        checks = json.dumps([{"name": "build", "state": "in_progress", "conclusion": None}])
        with patch("aya.ci._run", return_value=(0, checks)):
            status, failed = _poll_checks("42", max_wait=60, interval=30, sleep_fn=_noop_sleep)
        assert status == "timeout"
        assert failed == []

    def test_gh_failure_retries(self):
        """Non-zero gh exit should keep polling, not crash."""
        calls = [0]

        def fake_run(cmd):
            calls[0] += 1
            if calls[0] < 3:
                return (1, "")
            return (0, json.dumps([{"name": "x", "state": "completed", "conclusion": "success"}]))

        with patch("aya.ci._run", side_effect=fake_run):
            status, _failed = _poll_checks("42", max_wait=90, interval=30, sleep_fn=_noop_sleep)
        assert status == "pass"

    def test_cancelled_counts_as_failure(self):
        checks = json.dumps([{"name": "deploy", "state": "completed", "conclusion": "cancelled"}])
        with patch("aya.ci._run", return_value=(0, checks)):
            status, failed = _poll_checks("42", sleep_fn=_noop_sleep)
        assert status == "fail"
        assert "deploy" in failed


# ── watch_pr_checks ───────────────────────────────────────────────────────────


class TestWatchPrChecks:
    def _push_payload(self, cmd: str = "git push origin HEAD") -> dict:
        return {"tool_input": {"command": cmd}}

    def test_non_push_command_exits_0(self):
        rc = watch_pr_checks({"tool_input": {"command": "git status"}}, sleep_fn=_noop_sleep)
        assert rc == 0

    def test_empty_payload_exits_0(self):
        rc = watch_pr_checks({}, sleep_fn=_noop_sleep)
        assert rc == 0

    def test_non_github_remote_exits_0(self):
        with patch("aya.ci._run", return_value=(0, "git@gitlab.com:org/repo.git")):
            rc = watch_pr_checks(self._push_payload(), sleep_fn=_noop_sleep)
        assert rc == 0

    def test_main_branch_exits_0(self):
        def fake_run(cmd):
            if "remote" in cmd:
                return (0, "git@github.com:org/repo.git")
            if "branch" in cmd:
                return (0, "main")
            return (0, "")

        with patch("aya.ci._run", side_effect=fake_run):
            rc = watch_pr_checks(self._push_payload(), sleep_fn=_noop_sleep)
        assert rc == 0

    def test_master_branch_exits_0(self):
        def fake_run(cmd):
            if "remote" in cmd:
                return (0, "https://github.com/org/repo.git")
            if "branch" in cmd:
                return (0, "master")
            return (0, "")

        with patch("aya.ci._run", side_effect=fake_run):
            rc = watch_pr_checks(self._push_payload(), sleep_fn=_noop_sleep)
        assert rc == 0

    def test_no_pr_found_exits_0(self):
        def fake_run(cmd):
            if "remote" in cmd:
                return (0, "https://github.com/org/repo.git")
            if "branch" in cmd:
                return (0, "feat/my-feature")
            return (1, "")  # gh pr view fails — no PR

        with patch("aya.ci._run", side_effect=fake_run):
            rc = watch_pr_checks(self._push_payload(), sleep_fn=_noop_sleep)
        assert rc == 0

    def test_checks_pass_exits_0(self, capsys):
        def fake_run(cmd):
            if "remote" in cmd:
                return (0, "https://github.com/org/repo.git")
            if "branch" in cmd:
                return (0, "feat/my-feature")
            if "pr" in cmd and "view" in cmd:
                return (0, "99")
            if "pr" in cmd and "checks" in cmd:
                return (
                    0,
                    json.dumps([{"name": "ci", "state": "completed", "conclusion": "success"}]),
                )
            return (0, "")

        with patch("aya.ci._run", side_effect=fake_run):
            rc = watch_pr_checks(self._push_payload(), sleep_fn=_noop_sleep)
        assert rc == 0
        assert capsys.readouterr().out == ""

    def test_checks_fail_exits_2(self, capsys):
        def fake_run(cmd):
            if "remote" in cmd:
                return (0, "https://github.com/org/repo.git")
            if "branch" in cmd:
                return (0, "feat/my-feature")
            if "pr" in cmd and "view" in cmd:
                return (0, "42")
            if "pr" in cmd and "checks" in cmd:
                return (
                    0,
                    json.dumps([{"name": "lint", "state": "completed", "conclusion": "failure"}]),
                )
            return (0, "")

        with patch("aya.ci._run", side_effect=fake_run):
            rc = watch_pr_checks(self._push_payload(), sleep_fn=_noop_sleep)

        assert rc == 2
        out = capsys.readouterr().out
        payload = json.loads(out)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert "PR #42" in ctx
        assert "lint" in ctx
        assert "feat/my-feature" in ctx

    def test_checks_timeout_exits_2(self, capsys):
        def fake_run(cmd):
            if "remote" in cmd:
                return (0, "https://github.com/org/repo.git")
            if "branch" in cmd:
                return (0, "feat/timeout-test")
            if "pr" in cmd and "view" in cmd:
                return (0, "77")
            return (0, "")

        # Patch _poll_checks directly to return timeout without looping.
        with (
            patch("aya.ci._run", side_effect=fake_run),
            patch("aya.ci._poll_checks", return_value=("timeout", [])),
        ):
            rc = watch_pr_checks(self._push_payload(), sleep_fn=_noop_sleep)

        assert rc == 2
        out = capsys.readouterr().out
        payload = json.loads(out)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert "still running" in ctx
