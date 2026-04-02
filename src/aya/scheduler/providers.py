"""Watch providers — GitHub PR, Jira query, Jira ticket polling and change detection."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable
from typing import Any, cast

from .types import (
    GithubPrConfig,
    GithubPrState,
    JiraQueryConfig,
    JiraQueryState,
    JiraTicketConfig,
    JiraTicketState,
    SchedulerItem,
    WatchState,
)

logger = logging.getLogger(__name__)

# ── Jira credentials ─────────────────────────────────────────────────────────


def _get_jira_credentials() -> tuple[str, str, str]:
    """Extract Jira credentials from environment. Returns (email, token, server)."""
    email = os.environ.get("ATLASSIAN_EMAIL", "")
    token = os.environ.get("ATLASSIAN_API_TOKEN", "")
    server = os.environ.get("ATLASSIAN_SERVER_URL", "").rstrip("/")
    return email, token, server


# ── watch providers ──────────────────────────────────────────────────────────


def _run_gh(args: list[str], timeout: int = 15) -> dict[str, Any] | list[Any] | None:
    """Run gh CLI and parse JSON output."""
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout) if result.stdout.strip() else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        logging.debug("gh command failed: %s", e)
        return None


def _check_github_pr(config: GithubPrConfig) -> GithubPrState | None:
    """Check GitHub PR status and reviews."""
    owner = config["owner"]
    repo = config["repo"]
    pr = config["pr"]

    pr_data = _run_gh(
        [
            "api",
            f"/repos/{owner}/{repo}/pulls/{pr}",
            "--jq",
            "{ state: .state, merged: .merged, draft: .draft, title: .title }",
        ]
    )
    if not pr_data or not isinstance(pr_data, dict):
        return None

    reviews_raw = _run_gh(
        [
            "api",
            f"/repos/{owner}/{repo}/pulls/{pr}/reviews",
            "--jq",
            "[.[] | { user: .user.login, state: .state }]",
        ]
    )
    reviews: list[dict[str, Any]] = reviews_raw if isinstance(reviews_raw, list) else []

    return GithubPrState(
        pr_state=pr_data.get("state"),
        merged=pr_data.get("merged", False),
        draft=pr_data.get("draft", False),
        title=pr_data.get("title", ""),
        reviews=reviews,
        has_approval=any(r.get("state") == "APPROVED" for r in reviews),
    )


def _check_jira_query(config: JiraQueryConfig) -> JiraQueryState | None:
    """Run a JQL query and return results."""
    jql = config["jql"]
    email, token, server = _get_jira_credentials()

    if not all([email, token, server]):
        return None

    try:
        import httpx

        resp = httpx.post(
            f"{server}/rest/api/3/search",
            auth=(email, token),
            json={"jql": jql, "maxResults": 20, "fields": ["key", "summary", "status"]},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return {
            "total": data.get("total", 0),
            "issues": [
                {
                    "key": i["key"],
                    "summary": i["fields"]["summary"],
                    "status": i["fields"]["status"]["name"],
                }
                for i in data.get("issues", [])
            ],
        }
    except Exception as e:
        logging.debug("Jira query failed: %s", e)
        return None


def _check_jira_ticket(config: JiraTicketConfig) -> JiraTicketState | None:
    """Check a specific Jira ticket's status."""
    ticket = config["ticket"]
    email, token, server = _get_jira_credentials()

    if not all([email, token, server]):
        return None

    try:
        import httpx

        resp = httpx.get(
            f"{server}/rest/api/3/issue/{ticket}",
            auth=(email, token),
            params={"fields": "summary,status,assignee,priority"},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        fields = data.get("fields", {})
        return {
            "key": data["key"],
            "summary": fields.get("summary", ""),
            "status": fields.get("status", {}).get("name", ""),
            "assignee": (fields.get("assignee") or {}).get("displayName", "Unassigned"),
        }
    except Exception as e:
        logging.debug("Jira ticket check failed: %s", e)
        return None


WATCH_PROVIDERS: dict[str, Callable[..., WatchState | None]] = {
    "github-pr": _check_github_pr,
    "jira-query": _check_jira_query,
    "jira-ticket": _check_jira_ticket,
}


# ── change detection strategies ──────────────────────────────────────────────


def _detect_json_diff(new: WatchState, last: WatchState | None) -> bool:
    """Detect change by comparing JSON dumps."""
    return json.dumps(new, sort_keys=True) != json.dumps(last, sort_keys=True)


def _detect_github_approved_or_merged(new: GithubPrState, last: GithubPrState | None) -> bool:
    """Detect if PR was approved or merged."""
    was_approved = last["has_approval"] if last else False
    was_merged = last["merged"] if last else False
    return (new["has_approval"] and not was_approved) or (new["merged"] and not was_merged)


def _detect_github_merged(new: GithubPrState, last: GithubPrState | None) -> bool:
    """Detect if PR was merged."""
    return new["merged"] and not (last["merged"] if last else False)


def _detect_jira_new_results(new: JiraQueryState, last: JiraQueryState | None) -> bool:
    """Detect new issues in Jira query results."""
    old_keys = {i["key"] for i in last["issues"]} if last else set()
    new_keys = {i["key"] for i in new["issues"]}
    return bool(new_keys - old_keys)


def _detect_jira_count_change(new: JiraQueryState, last: JiraQueryState | None) -> bool:
    """Detect change in Jira query result count."""
    return new["total"] != (last["total"] if last else 0)


def _detect_jira_status_changed(new: JiraTicketState, last: JiraTicketState | None) -> bool:
    """Detect if Jira ticket status changed."""
    return new["status"] != (last["status"] if last else None)


_CHANGE_DETECTORS: dict[tuple[str, str], Callable[[Any, Any], bool]] = {
    ("github-pr", "approved_or_merged"): _detect_github_approved_or_merged,
    ("github-pr", "merged"): _detect_github_merged,
    ("github-pr", ""): _detect_json_diff,
    ("jira-query", "new_results"): _detect_jira_new_results,
    ("jira-query", ""): _detect_jira_count_change,
    ("jira-ticket", "status_changed"): _detect_jira_status_changed,
    ("jira-ticket", ""): _detect_json_diff,
}


def poll_watch(item: SchedulerItem) -> tuple[WatchState | None, bool]:
    """Poll a watch item. Returns (new_state, changed)."""
    provider = item.get("provider", "")
    check_fn = WATCH_PROVIDERS.get(provider)
    if not check_fn:
        return None, False

    watch_config = item.get("watch_config")
    if watch_config is None:
        return None, False
    new_state = check_fn(watch_config)
    if new_state is None:
        return None, False

    last_state = item.get("last_state")
    condition = item.get("condition", "")

    # Use strategy dict to detect changes
    detector = _CHANGE_DETECTORS.get((provider, condition))
    changed = detector(new_state, last_state) if detector else False

    return new_state, changed


def _evaluate_auto_remove(item: SchedulerItem, state: WatchState) -> bool:
    """Check if a watch should be auto-removed based on remove_when condition."""
    remove_when = item.get("remove_when", "")
    if not remove_when:
        return False
    if remove_when == "merged_or_closed" and item.get("provider") == "github-pr":
        gh_state = cast(GithubPrState, state)
        return gh_state["merged"] or gh_state["pr_state"] == "closed"
    return False
