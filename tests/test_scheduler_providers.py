"""Tests for scheduler/providers.py — watch provider polling and change detection."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from aya.scheduler.providers import (
    _check_ci_checks,
    _check_github_pr,
    _check_jira_query,
    _check_jira_ticket,
    _detect_ci_checks_complete,
    _detect_ci_checks_failed,
    _detect_github_approved_or_merged,
    _detect_github_merged,
    _detect_jira_count_change,
    _detect_jira_new_results,
    _detect_jira_status_changed,
    _detect_json_diff,
    _evaluate_auto_remove,
    _get_jira_credentials,
    _run_gh,
    poll_watch,
)
from aya.scheduler.types import (
    CiChecksState,
    GithubPrState,
    JiraQueryState,
    JiraTicketState,
    SchedulerItem,
)

# ── _get_jira_credentials ────────────────────────────────────────────────────


class TestGetJiraCredentials:
    def test_returns_empty_strings_when_not_set(self, monkeypatch):
        monkeypatch.delenv("ATLASSIAN_EMAIL", raising=False)
        monkeypatch.delenv("ATLASSIAN_API_TOKEN", raising=False)
        monkeypatch.delenv("ATLASSIAN_SERVER_URL", raising=False)
        email, token, server = _get_jira_credentials()
        assert email == ""
        assert token == ""
        assert server == ""

    def test_returns_credentials_from_env(self, monkeypatch):
        monkeypatch.setenv("ATLASSIAN_EMAIL", "user@example.com")
        monkeypatch.setenv("ATLASSIAN_API_TOKEN", "secret-token")
        monkeypatch.setenv("ATLASSIAN_SERVER_URL", "https://jira.example.com/")
        email, token, server = _get_jira_credentials()
        assert email == "user@example.com"
        assert token == "secret-token"
        assert server == "https://jira.example.com"  # trailing slash stripped

    def test_strips_trailing_slash_from_server(self, monkeypatch):
        monkeypatch.setenv("ATLASSIAN_SERVER_URL", "https://jira.example.com///")
        _, _, server = _get_jira_credentials()
        assert not server.endswith("/")


# ── _run_gh ──────────────────────────────────────────────────────────────────


class TestRunGh:
    def test_returns_dict_on_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"key": "value"}'
        with patch("subprocess.run", return_value=mock_result):
            result = _run_gh(["api", "/repos/owner/repo"])
        assert result == {"key": "value"}

    def test_returns_list_on_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '[{"id": 1}, {"id": 2}]'
        with patch("subprocess.run", return_value=mock_result):
            result = _run_gh(["api", "/repos/owner/repo/pulls"])
        assert isinstance(result, list)
        assert len(result) == 2

    def test_returns_none_on_nonzero_exit(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            result = _run_gh(["api", "/repos/owner/repo"])
        assert result is None

    def test_returns_none_on_empty_stdout(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "   "
        with patch("subprocess.run", return_value=mock_result):
            result = _run_gh(["api", "/repos/owner/repo"])
        assert result is None

    def test_returns_none_when_gh_missing(self):
        import aya.scheduler.providers as prov_mod

        prov_mod._gh_missing_warned = False
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = _run_gh(["api", "/anything"])
        assert result is None

    def test_returns_none_on_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 15)):
            result = _run_gh(["api", "/repos/owner/repo"])
        assert result is None

    def test_returns_none_on_json_decode_error(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not-json"
        with patch("subprocess.run", return_value=mock_result):
            result = _run_gh(["api", "/repos/owner/repo"])
        assert result is None

    def test_gh_missing_warning_only_once(self, caplog):
        import aya.scheduler.providers as prov_mod

        prov_mod._gh_missing_warned = False
        import logging

        with (
            caplog.at_level(logging.WARNING, logger="aya.scheduler.providers"),
            patch("subprocess.run", side_effect=FileNotFoundError),
        ):
            _run_gh(["api", "/a"])
            _run_gh(["api", "/b"])
        warning_msgs = [r for r in caplog.records if "not installed" in r.message]
        assert len(warning_msgs) == 1


# ── _check_github_pr ─────────────────────────────────────────────────────────


class TestCheckGithubPr:
    def _pr_config(self):
        return {"owner": "acme", "repo": "widget", "pr": 42}

    def test_returns_none_when_gh_fails(self):
        with patch("aya.scheduler.providers._run_gh", return_value=None):
            result = _check_github_pr(self._pr_config())
        assert result is None

    def test_returns_none_when_pr_data_not_dict(self):
        with patch("aya.scheduler.providers._run_gh", side_effect=[[{"id": 1}], []]):
            result = _check_github_pr(self._pr_config())
        assert result is None

    def test_open_pr_with_no_reviews(self):
        pr_data = {"state": "open", "merged": False, "draft": False, "title": "My PR"}
        with patch(
            "aya.scheduler.providers._run_gh",
            side_effect=[pr_data, []],
        ):
            result = _check_github_pr(self._pr_config())
        assert result is not None
        assert result["pr_state"] == "open"
        assert result["merged"] is False
        assert result["has_approval"] is False
        assert result["reviews"] == []

    def test_approved_pr(self):
        pr_data = {"state": "open", "merged": False, "draft": False, "title": "My PR"}
        reviews = [{"user": "alice", "state": "APPROVED"}]
        with patch(
            "aya.scheduler.providers._run_gh",
            side_effect=[pr_data, reviews],
        ):
            result = _check_github_pr(self._pr_config())
        assert result is not None
        assert result["has_approval"] is True

    def test_merged_pr(self):
        pr_data = {"state": "closed", "merged": True, "draft": False, "title": "My PR"}
        with patch(
            "aya.scheduler.providers._run_gh",
            side_effect=[pr_data, []],
        ):
            result = _check_github_pr(self._pr_config())
        assert result is not None
        assert result["merged"] is True


# ── _check_jira_query ────────────────────────────────────────────────────────


class TestCheckJiraQuery:
    def test_returns_none_without_credentials(self, monkeypatch):
        monkeypatch.delenv("ATLASSIAN_EMAIL", raising=False)
        monkeypatch.delenv("ATLASSIAN_API_TOKEN", raising=False)
        monkeypatch.delenv("ATLASSIAN_SERVER_URL", raising=False)
        result = _check_jira_query({"jql": "project = TEST"})
        assert result is None

    def test_returns_state_on_success(self, monkeypatch):
        monkeypatch.setenv("ATLASSIAN_EMAIL", "u@example.com")
        monkeypatch.setenv("ATLASSIAN_API_TOKEN", "tok")
        monkeypatch.setenv("ATLASSIAN_SERVER_URL", "https://jira.example.com")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "total": 2,
            "issues": [
                {
                    "key": "TEST-1",
                    "fields": {"summary": "First issue", "status": {"name": "Open"}},
                },
                {
                    "key": "TEST-2",
                    "fields": {"summary": "Second issue", "status": {"name": "In Progress"}},
                },
            ],
        }
        with patch("httpx.post", return_value=mock_resp):
            result = _check_jira_query({"jql": "project = TEST"})
        assert result is not None
        assert result["total"] == 2
        assert len(result["issues"]) == 2
        assert result["issues"][0]["key"] == "TEST-1"

    def test_returns_none_on_non_200(self, monkeypatch):
        monkeypatch.setenv("ATLASSIAN_EMAIL", "u@example.com")
        monkeypatch.setenv("ATLASSIAN_API_TOKEN", "tok")
        monkeypatch.setenv("ATLASSIAN_SERVER_URL", "https://jira.example.com")
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        with patch("httpx.post", return_value=mock_resp):
            result = _check_jira_query({"jql": "project = TEST"})
        assert result is None

    def test_returns_none_on_exception(self, monkeypatch):
        monkeypatch.setenv("ATLASSIAN_EMAIL", "u@example.com")
        monkeypatch.setenv("ATLASSIAN_API_TOKEN", "tok")
        monkeypatch.setenv("ATLASSIAN_SERVER_URL", "https://jira.example.com")
        with patch("httpx.post", side_effect=Exception("connection failed")):
            result = _check_jira_query({"jql": "project = TEST"})
        assert result is None


# ── _check_jira_ticket ───────────────────────────────────────────────────────


class TestCheckJiraTicket:
    def test_returns_none_without_credentials(self, monkeypatch):
        monkeypatch.delenv("ATLASSIAN_EMAIL", raising=False)
        monkeypatch.delenv("ATLASSIAN_API_TOKEN", raising=False)
        monkeypatch.delenv("ATLASSIAN_SERVER_URL", raising=False)
        result = _check_jira_ticket({"ticket": "CSD-123"})
        assert result is None

    def test_returns_state_on_success(self, monkeypatch):
        monkeypatch.setenv("ATLASSIAN_EMAIL", "u@example.com")
        monkeypatch.setenv("ATLASSIAN_API_TOKEN", "tok")
        monkeypatch.setenv("ATLASSIAN_SERVER_URL", "https://jira.example.com")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "key": "CSD-123",
            "fields": {
                "summary": "My ticket",
                "status": {"name": "In Review"},
                "assignee": {"displayName": "Alice"},
                "priority": {"name": "High"},
            },
        }
        with patch("httpx.get", return_value=mock_resp):
            result = _check_jira_ticket({"ticket": "CSD-123"})
        assert result is not None
        assert result["key"] == "CSD-123"
        assert result["status"] == "In Review"
        assert result["assignee"] == "Alice"

    def test_unassigned_ticket(self, monkeypatch):
        monkeypatch.setenv("ATLASSIAN_EMAIL", "u@example.com")
        monkeypatch.setenv("ATLASSIAN_API_TOKEN", "tok")
        monkeypatch.setenv("ATLASSIAN_SERVER_URL", "https://jira.example.com")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "key": "CSD-456",
            "fields": {
                "summary": "Unassigned ticket",
                "status": {"name": "Open"},
                "assignee": None,
            },
        }
        with patch("httpx.get", return_value=mock_resp):
            result = _check_jira_ticket({"ticket": "CSD-456"})
        assert result is not None
        assert result["assignee"] == "Unassigned"

    def test_returns_none_on_non_200(self, monkeypatch):
        monkeypatch.setenv("ATLASSIAN_EMAIL", "u@example.com")
        monkeypatch.setenv("ATLASSIAN_API_TOKEN", "tok")
        monkeypatch.setenv("ATLASSIAN_SERVER_URL", "https://jira.example.com")
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch("httpx.get", return_value=mock_resp):
            result = _check_jira_ticket({"ticket": "CSD-999"})
        assert result is None

    def test_returns_none_on_exception(self, monkeypatch):
        monkeypatch.setenv("ATLASSIAN_EMAIL", "u@example.com")
        monkeypatch.setenv("ATLASSIAN_API_TOKEN", "tok")
        monkeypatch.setenv("ATLASSIAN_SERVER_URL", "https://jira.example.com")
        with patch("httpx.get", side_effect=Exception("timeout")):
            result = _check_jira_ticket({"ticket": "CSD-123"})
        assert result is None


# ── _check_ci_checks ─────────────────────────────────────────────────────────


class TestCheckCiChecks:
    def _config(self):
        return {"owner": "acme", "repo": "widget", "pr": 42}

    def test_returns_none_when_gh_fails(self):
        with patch("aya.scheduler.providers._run_gh", return_value=None):
            result = _check_ci_checks(self._config())
        assert result is None

    def test_returns_none_when_not_list(self):
        with patch("aya.scheduler.providers._run_gh", return_value={"not": "list"}):
            result = _check_ci_checks(self._config())
        assert result is None

    def test_all_passed(self):
        checks = [
            {"name": "lint", "state": "completed", "conclusion": "success"},
            {"name": "test", "state": "completed", "conclusion": "success"},
        ]
        with patch("aya.scheduler.providers._run_gh", return_value=checks):
            result = _check_ci_checks(self._config())
        assert result is not None
        assert result["all_complete"] is True
        assert result["passed"] == ["lint", "test"]
        assert result["failed"] == []
        assert result["pending"] == []

    def test_some_failed(self):
        checks = [
            {"name": "lint", "state": "completed", "conclusion": "success"},
            {"name": "test", "state": "completed", "conclusion": "failure"},
        ]
        with patch("aya.scheduler.providers._run_gh", return_value=checks):
            result = _check_ci_checks(self._config())
        assert result is not None
        assert result["all_complete"] is True
        assert "test" in result["failed"]
        assert "lint" in result["passed"]

    def test_some_pending(self):
        checks = [
            {"name": "build", "state": "in_progress", "conclusion": None},
            {"name": "test", "state": "completed", "conclusion": "success"},
        ]
        with patch("aya.scheduler.providers._run_gh", return_value=checks):
            result = _check_ci_checks(self._config())
        assert result is not None
        assert result["all_complete"] is False
        assert "build" in result["pending"]

    def test_timed_out_check_goes_to_failed(self):
        checks = [{"name": "slow-test", "state": "completed", "conclusion": "timed_out"}]
        with patch("aya.scheduler.providers._run_gh", return_value=checks):
            result = _check_ci_checks(self._config())
        assert result is not None
        assert "slow-test" in result["failed"]

    def test_cancelled_check_goes_to_failed(self):
        checks = [{"name": "deploy", "state": "completed", "conclusion": "cancelled"}]
        with patch("aya.scheduler.providers._run_gh", return_value=checks):
            result = _check_ci_checks(self._config())
        assert result is not None
        assert "deploy" in result["failed"]


# ── change detectors ─────────────────────────────────────────────────────────


class TestDetectJsonDiff:
    def test_same_state_no_change(self):
        state = {"key": "value"}
        assert _detect_json_diff(state, state) is False

    def test_different_state_detected(self):
        assert _detect_json_diff({"key": "new"}, {"key": "old"}) is True

    def test_none_last_triggers_change(self):
        assert _detect_json_diff({"key": "val"}, None) is True


class TestDetectGithubApprovedOrMerged:
    def _state(self, *, has_approval=False, merged=False):
        return GithubPrState(
            pr_state="open",
            merged=merged,
            draft=False,
            title="PR",
            reviews=[],
            has_approval=has_approval,
        )

    def test_no_change(self):
        new = self._state(has_approval=True)
        last = self._state(has_approval=True)
        assert _detect_github_approved_or_merged(new, last) is False

    def test_newly_approved(self):
        new = self._state(has_approval=True)
        last = self._state(has_approval=False)
        assert _detect_github_approved_or_merged(new, last) is True

    def test_newly_merged(self):
        new = self._state(merged=True)
        last = self._state(merged=False)
        assert _detect_github_approved_or_merged(new, last) is True

    def test_no_last_state(self):
        new = self._state(has_approval=True)
        assert _detect_github_approved_or_merged(new, None) is True


class TestDetectGithubMerged:
    def _state(self, merged=False):
        return GithubPrState(
            pr_state="open", merged=merged, draft=False, title="PR", reviews=[], has_approval=False
        )

    def test_not_merged(self):
        assert _detect_github_merged(self._state(merged=False), None) is False

    def test_newly_merged(self):
        new = self._state(merged=True)
        last = self._state(merged=False)
        assert _detect_github_merged(new, last) is True

    def test_already_merged_no_change(self):
        state = self._state(merged=True)
        assert _detect_github_merged(state, state) is False


class TestDetectJiraNewResults:
    def test_no_new_issues(self):
        state: JiraQueryState = {
            "total": 1,
            "issues": [{"key": "A-1", "summary": "x", "status": "Open"}],
        }
        assert _detect_jira_new_results(state, state) is False

    def test_new_issue_detected(self):
        old: JiraQueryState = {
            "total": 1,
            "issues": [{"key": "A-1", "summary": "x", "status": "Open"}],
        }
        new: JiraQueryState = {
            "total": 2,
            "issues": [
                {"key": "A-1", "summary": "x", "status": "Open"},
                {"key": "A-2", "summary": "y", "status": "Open"},
            ],
        }
        assert _detect_jira_new_results(new, old) is True

    def test_none_last_with_issues_triggers(self):
        new: JiraQueryState = {
            "total": 1,
            "issues": [{"key": "A-1", "summary": "x", "status": "Open"}],
        }
        assert _detect_jira_new_results(new, None) is True


class TestDetectJiraCountChange:
    def test_same_count_no_change(self):
        state: JiraQueryState = {"total": 5, "issues": []}
        assert _detect_jira_count_change(state, state) is False

    def test_count_increased(self):
        old: JiraQueryState = {"total": 3, "issues": []}
        new: JiraQueryState = {"total": 5, "issues": []}
        assert _detect_jira_count_change(new, old) is True

    def test_none_last_with_nonzero_count(self):
        new: JiraQueryState = {"total": 2, "issues": []}
        assert _detect_jira_count_change(new, None) is True

    def test_none_last_with_zero_count(self):
        new: JiraQueryState = {"total": 0, "issues": []}
        assert _detect_jira_count_change(new, None) is False


class TestDetectJiraStatusChanged:
    def test_same_status(self):
        state: JiraTicketState = {"key": "A-1", "summary": "x", "status": "Open", "assignee": "u"}
        assert _detect_jira_status_changed(state, state) is False

    def test_status_changed(self):
        old: JiraTicketState = {"key": "A-1", "summary": "x", "status": "Open", "assignee": "u"}
        new: JiraTicketState = {
            "key": "A-1",
            "summary": "x",
            "status": "In Review",
            "assignee": "u",
        }
        assert _detect_jira_status_changed(new, old) is True

    def test_none_last_with_status(self):
        new: JiraTicketState = {
            "key": "A-1",
            "summary": "x",
            "status": "Open",
            "assignee": "u",
        }
        assert _detect_jira_status_changed(new, None) is True


class TestDetectCiChecks:
    def _state(self, *, all_complete=True, failed=None, passed=None, pending=None):
        return CiChecksState(
            all_complete=all_complete,
            passed=passed or [],
            failed=failed or [],
            pending=pending or [],
        )

    def test_checks_failed_when_complete_with_failures(self):
        state = self._state(all_complete=True, failed=["test"])
        assert _detect_ci_checks_failed(state, None) is True

    def test_checks_not_failed_when_pending(self):
        state = self._state(all_complete=False, failed=["test"])
        assert _detect_ci_checks_failed(state, None) is False

    def test_checks_not_failed_when_no_failures(self):
        state = self._state(all_complete=True, failed=[])
        assert _detect_ci_checks_failed(state, None) is False

    def test_checks_complete_when_all_done(self):
        state = self._state(all_complete=True)
        assert _detect_ci_checks_complete(state, None) is True

    def test_checks_incomplete(self):
        state = self._state(all_complete=False)
        assert _detect_ci_checks_complete(state, None) is False


# ── poll_watch ───────────────────────────────────────────────────────────────


class TestPollWatch:
    def _item(self, provider="github-pr", condition="approved_or_merged", last_state=None):
        item: SchedulerItem = {
            "id": "01JTEST00000000000000000001",
            "type": "watch",
            "status": "active",
            "message": "Test watch",
            "provider": provider,
            "watch_config": {"owner": "acme", "repo": "widget", "pr": 42},
            "condition": condition,
            "last_state": last_state,
            "created_at": "2026-01-01T00:00:00",
        }
        return item

    def test_unknown_provider_returns_none_false(self):
        item = self._item(provider="unknown-provider")
        state, changed = poll_watch(item)
        assert state is None
        assert changed is False

    def test_missing_watch_config_returns_none_false(self):
        item = self._item()
        del item["watch_config"]
        state, changed = poll_watch(item)
        assert state is None
        assert changed is False

    def test_provider_returns_none_no_change(self):
        item = self._item()
        with patch.dict("aya.scheduler.providers.WATCH_PROVIDERS", {"github-pr": lambda cfg: None}):
            state, changed = poll_watch(item)
        assert state is None
        assert changed is False

    def test_no_change_detected(self):
        pr_state = GithubPrState(
            pr_state="open", merged=False, draft=False, title="PR", reviews=[], has_approval=False
        )
        item = self._item(condition="approved_or_merged", last_state=pr_state)
        with patch.dict(
            "aya.scheduler.providers.WATCH_PROVIDERS", {"github-pr": lambda cfg: pr_state}
        ):
            state, changed = poll_watch(item)
        assert state is not None
        assert changed is False

    def test_change_detected(self):
        old_state = GithubPrState(
            pr_state="open", merged=False, draft=False, title="PR", reviews=[], has_approval=False
        )
        new_state = GithubPrState(
            pr_state="open",
            merged=False,
            draft=False,
            title="PR",
            reviews=[{"user": "alice", "state": "APPROVED"}],
            has_approval=True,
        )
        item = self._item(condition="approved_or_merged", last_state=old_state)
        with patch.dict(
            "aya.scheduler.providers.WATCH_PROVIDERS", {"github-pr": lambda cfg: new_state}
        ):
            state, changed = poll_watch(item)
        assert state is not None
        assert changed is True


# ── _evaluate_auto_remove ────────────────────────────────────────────────────


class TestEvaluateAutoRemove:
    def _pr_state(self, *, merged=False, pr_state="open"):
        return GithubPrState(
            pr_state=pr_state,
            merged=merged,
            draft=False,
            title="PR",
            reviews=[],
            has_approval=False,
        )

    def _item(self, provider="github-pr", remove_when=""):
        item: SchedulerItem = {
            "id": "01JTEST00000000000000000001",
            "type": "watch",
            "status": "active",
            "message": "Test watch",
            "provider": provider,
            "watch_config": {"owner": "acme", "repo": "widget", "pr": 42},
            "condition": "",
            "created_at": "2026-01-01T00:00:00",
        }
        if remove_when:
            item["remove_when"] = remove_when
        return item

    def test_no_remove_when_returns_false(self):
        item = self._item(remove_when="")
        assert _evaluate_auto_remove(item, self._pr_state()) is False

    def test_merged_pr_triggers_removal(self):
        item = self._item(provider="github-pr", remove_when="merged_or_closed")
        assert _evaluate_auto_remove(item, self._pr_state(merged=True)) is True

    def test_closed_pr_triggers_removal(self):
        item = self._item(provider="github-pr", remove_when="merged_or_closed")
        assert _evaluate_auto_remove(item, self._pr_state(pr_state="closed")) is True

    def test_open_pr_no_removal(self):
        item = self._item(provider="github-pr", remove_when="merged_or_closed")
        assert _evaluate_auto_remove(item, self._pr_state(pr_state="open")) is False

    def test_ci_checks_complete_triggers_removal(self):
        item = self._item(provider="ci-checks", remove_when="checks_complete")
        ci_state = CiChecksState(all_complete=True, passed=["lint"], failed=[], pending=[])
        assert _evaluate_auto_remove(item, ci_state) is True

    def test_ci_checks_incomplete_no_removal(self):
        item = self._item(provider="ci-checks", remove_when="checks_complete")
        ci_state = CiChecksState(all_complete=False, passed=[], failed=[], pending=["test"])
        assert _evaluate_auto_remove(item, ci_state) is False

    def test_unknown_remove_when_returns_false(self):
        item = self._item(provider="github-pr", remove_when="some_unknown_condition")
        assert _evaluate_auto_remove(item, self._pr_state()) is False
