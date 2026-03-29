---
name: aya-watch
description: >
  Add a watch on a GitHub PR or other target with smart defaults. Invoke when
  the user says "watch this PR", "track PR 123", "let me know when PR merges",
  "watch for approval", or "monitor this".
argument-hint: "<PR reference, e.g. owner/repo#123 or just #123>"
---

# Watch

Add a watch on a GitHub PR (or other target) with sensible defaults.

---

## Steps

1. **Resolve the target.** Parse what the user wants to watch:

   - If they gave `owner/repo#123` or a full GitHub URL, extract the PR reference.
   - If they gave just `#123`, detect the current repo from `gh repo view --json nameWithOwner -q .nameWithOwner` and build `owner/repo#123`.
   - If they gave a branch name, find the open PR for that branch via `gh pr list --head {branch} --json number,url -q '.[0]'`.

2. **Determine the provider.** Based on the target:

   | Target pattern | Provider |
   |---|---|
   | `owner/repo#N` or GitHub URL | `github-pr` |
   | `PROJ-123` (Jira-style key) | `jira-ticket` |

3. **Set defaults.** For GitHub PRs:
   - `--message`: "PR {number} — {title}" (fetch title via `gh pr view {number} --json title -q .title`)
   - `--remove-when merged_or_closed`
   - `--interval 5` (5-minute polling for PRs)

4. **Confirm.** Show the user what will be watched:

   ```
   Watch: {provider} {target}
   Message: {message}
   Auto-remove: when merged or closed
   Poll interval: {N} minutes
   ```

   Wait for confirmation or adjustments.

5. **Create the watch.**

   ```bash
   aya schedule watch {provider} {target} \
     -m "{message}" \
     --remove-when merged_or_closed \
     -i {interval}
   ```

6. **Confirm.** Report the watch ID and that it's active.

---

## Notes

- For PRs the user just opened or is reviewing, default to `--remove-when merged_or_closed` so the watch cleans itself up.
- If the user says "let me know when it's approved", add `--condition approved_or_merged`.
- Jira watches require `ATLASSIAN_*` env vars to be set. If they're missing, tell the user to run `op-load-env` or set them manually.
- Watch IDs support prefix matching for later dismiss/snooze operations.
