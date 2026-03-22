"""CI integration — watch PR checks after git push."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable

_POLL_INTERVAL = 30
_MAX_WAIT = 600


def _run(cmd: list[str]) -> tuple[int, str]:
    """Run a subprocess command, return (returncode, stdout)."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout.strip()


def _find_pr(branch: str, retries: int = 3, delay: int = 10) -> str | None:
    """Find the PR number for a branch. Returns number as string or None."""
    for _ in range(retries):
        rc, out = _run(["gh", "pr", "view", branch, "--json", "number", "-q", ".number"])
        if rc == 0 and out:
            return out
        time.sleep(delay)
    return None


def _poll_checks(
    pr_number: str,
    max_wait: int = _MAX_WAIT,
    interval: int = _POLL_INTERVAL,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[str, list[str]]:
    """Poll PR checks until all complete or timeout.

    Returns:
        ("pass", [])             — all checks green
        ("fail", [name, ...])    — one or more checks failed
        ("timeout", [])          — still pending after max_wait seconds
    """
    elapsed = 0
    while elapsed < max_wait:
        rc, out = _run(["gh", "pr", "checks", pr_number, "--json", "name,state,conclusion"])
        if rc != 0:
            sleep_fn(interval)
            elapsed += interval
            continue

        checks: list[dict] = json.loads(out) if out else []
        pending = [c for c in checks if c.get("state") in ("pending", "in_progress", "queued")]
        if not pending:
            failed = [
                c["name"]
                for c in checks
                if c.get("conclusion") in ("failure", "timed_out", "cancelled")
            ]
            return ("fail", failed) if failed else ("pass", [])

        sleep_fn(interval)
        elapsed += interval

    return "timeout", []


def watch_pr_checks(
    hook_payload: dict,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    """Main watch logic. Intended to be called with the Claude hook JSON payload.

    Returns:
        0 — nothing to do or all checks passed (silent)
        2 — CI failed or timed out (wake the model)
    """
    command = hook_payload.get("tool_input", {}).get("command", "")

    if "git push" not in command:
        return 0

    rc, remote = _run(["git", "remote", "get-url", "origin"])
    if rc != 0 or "github.com" not in remote:
        return 0

    rc, branch = _run(["git", "branch", "--show-current"])
    if rc != 0 or not branch or branch in ("main", "master"):
        return 0

    # Give GitHub a moment to register the push before looking for a PR.
    sleep_fn(5)

    pr_number = _find_pr(branch)
    if not pr_number:
        return 0

    status, failed = _poll_checks(pr_number, sleep_fn=sleep_fn)

    if status == "fail":
        names = ", ".join(failed)
        _emit(
            f"CI FAILED on PR #{pr_number} (branch: {branch}). "
            f"Failed checks: {names} — notify the user and investigate."
        )
        return 2

    if status == "timeout":
        _emit(
            f"CI checks on PR #{pr_number} (branch: {branch}) still running "
            f"after 10 minutes — may need a manual check."
        )
        return 2

    return 0


def _emit(context: str) -> None:
    """Print asyncRewake JSON payload to stdout."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": context,
                }
            }
        )
    )
