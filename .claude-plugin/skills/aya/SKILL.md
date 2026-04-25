---
name: aya
description: >
  Manage aya — setup, pairing, status, refresh, and watches. Invoke when the user
  says "set up aya", "initialize aya", "pair with work", "trust another machine",
  "check aya", "aya status", "any alerts", "refresh aya", "update aya", "reinstall",
  "watch this PR", "track PR 123", "monitor this", or equivalent.
argument-hint: "[setup | pair | status | refresh | watch] [args]"
---

# Aya

Manage the aya assistant toolkit — identity, pairing, health, updates, and watches.

---

## Tool surface

When the aya MCP server is connected, prefer the `aya_*` tools listed
inline with each verb below. Fall back to the CLI when any of these apply:

- The MCP server isn't connected (no `aya_*` tools in the available set).
- The operation has no MCP equivalent — most of Setup (`aya init`,
  `aya schedule install`, `aya schedule recurring`), all of Pair,
  Refresh (`uv tool` commands), and `aya schedule dismiss`.
- The operation needs flags the MCP tool doesn't expose — e.g.
  `aya_schedule_watch` has no `--remove-when`, `-i`, or `--condition`,
  so Watch setup is CLI-preferred.

MCP tools that act on a local identity (e.g. `aya_relay_status`,
`aya_send`, `aya_receive`) take `instance=<label>` where the CLI takes
`--as <label>`. Tools like `aya_status`, `aya_schedule_watch`, and
`aya_config_show` don't take an identity argument.

---

## 0. Route intent

### Explicit subcommands

`/aya setup`, `/aya pair`, `/aya status`, `/aya refresh`, `/aya watch`
→ use the verb directly, no inference needed.

### Keyword routing

| User says | Verb |
|---|---|
| "set up aya", "initialize", "first time", "bootstrap", "new machine" | 1. Setup |
| "pair with work", "trust another machine", "connect my machines" | 2. Pair |
| "check aya", "aya status", "any alerts", "what's pending", "what's watching" | 3. Status |
| "refresh aya", "update aya", "reinstall aya" | 4. Refresh |
| "watch this PR", "track PR 123", "monitor this", "let me know when PR merges" | 5. Watch |

### Context inference (when keywords don't match)

| User says | Inferred verb + context |
|---|---|
| "is aya working?" | Status |
| "new laptop, need aya" | Setup |
| "get the latest aya" | Refresh |
| "keep an eye on that PR" | Watch |
| Just `/aya` with no context | Status, then ask "What do you need?" |

---

## 1. Setup

Full first-run bootstrap: identity, hooks, relay polling, and optional pairing.

Setup is **mostly CLI-only** — no MCP tools cover `aya init`,
`aya schedule install`, or `aya schedule recurring`. Only the initial
"check current state" step has an MCP equivalent.

### Steps

1. **Check current state.**
   - **MCP:** `aya_status()` — returns systems/alerts/watches summary.
   - **CLI:** `aya status`.

   If aya is fully set up (identity exists, hooks installed), report
   that and ask if the user wants to re-pair or reconfigure anything.
   Don't re-run setup unnecessarily.

2. **Initialize identity.** If no instance exists:

   ```bash
   aya init --label {label}
   ```

   Ask the user for a label if not provided (common choices: "home", "work", "laptop").

3. **Install hooks and crontab.**

   ```bash
   aya schedule install --dry-run
   ```

   Show the user what will be installed. If they approve:

   ```bash
   aya schedule install
   ```

4. **Set up relay polling.** Register a recurring relay poll so packets are received automatically:

   ```bash
   aya schedule recurring -m "relay-poll" -c "*/10 * * * *" \
     -p "Run: aya receive --as {label} --auto-ingest --skip-untrusted --quiet. If any packets were ingested, surface their content to the user."
   ```

5. **Set up health break reminder** (optional — ask if the user wants it):

   ```bash
   aya schedule recurring -m "health-break" -c "*/20 * * * *" \
     -p "Deliver a health break reminder in the Ship Mind voice. Output ONLY the reminder message itself — no preamble, no confirmation afterward. Suggest standing up, stretching, getting water, and walking for at least 2 minutes. Warm, brief, varied — two sentences max." \
     --idle-back-off 10m
   ```

   **Output style rule for progress/logging and reminder/health session crons:**
   - Progress/logging crons: prompt must end with "Output nothing. Silence is correct."
   - Reminder/health crons: prompt must include "Output ONLY the reminder message itself — no preamble, no confirmation afterward."
   - `relay-poll` is a special case: it may remain silent when nothing is ingested, but it may surface packet content to the user when packets are received.

6. **Wire up the skills.** Two patterns work — pick whichever the consuming workspace uses (the aya repo itself doesn't ship the wiring):

   - **Symlink pattern.** If the consuming workspace has its own `Makefile` with a `link-skills` target, symlink each skill's `SKILL.md` into `~/.claude/commands/`. Typical wiring: `<workspace>/skills/{aya,relay}/` are symlinks into the aya repo's `.claude-plugin/skills/{aya,relay}/`, and `make link-skills` exposes them as flat slash commands. A SessionStart hook can keep the links fresh.
   - **Plugin-dir alias (portable, no Makefile needed).** If there's no symlink workflow:
     ```bash
     alias claude='claude --plugin-dir /path/to/aya'
     ```
     Adjust the path to the local aya clone. Loads `/aya` and `/relay` under the plugin namespace.

7. **Offer pairing.** Ask: "Do you want to pair with another machine now?" If yes, hand off to verb 2 (Pair).

8. **Summary.** Report what was set up:

   ```
   aya setup complete:
   - Identity: {label} ({DID prefix}...)
   - Hooks: installed (SessionStart, PreToolUse, PostToolUse)
   - Crontab: aya schedule tick --quiet (default: every 5m)
   - Relay poll: every 10 minutes
   - Health break: every 20 minutes (with idle back-off)
   - Plugin: claude alias configured
   - Pairing: {paired with X | skipped}
   ```

### Notes

- This is idempotent — `aya schedule install` won't duplicate existing hooks.
- The system crontab runs `aya schedule tick --quiet` every 5 minutes for background polling.
- If the user already has an identity but no hooks, skip to step 3.
- If `aya` isn't installed at all, guide the user: `uv tool install aya-ai-assist`.

---

## 2. Pair

Pair is **CLI-only** (no `aya_pair` MCP tool). Walk through pairing this
aya instance with another machine.

### Steps

1. **Check identity exists.** Run `aya status`. If no instance is initialized, run `aya init --label {label}` first (ask the user what label to use — typically "home", "work", or "laptop").

2. **Determine role.** Ask the user:
   - **"Are you starting the pairing, or do you have a code from the other machine?"**
   - If starting (initiator): go to step 3.
   - If joining with a code: go to step 4.

3. **Initiator flow.**

   ```bash
   aya pair --peer {remote_label} --as {local_instance}
   ```

   This prints a pairing code (e.g., `ANCHOR-NORTH-0045`). Tell the user:

   > **Pairing code: `{CODE}`**
   > Enter this code on your other machine within 10 minutes.
   > Run: `aya pair --code {CODE} --peer {this_machine_label} --as {other_instance}`

   Then wait. The command will poll the relay for the response and complete automatically.

4. **Joiner flow.** The user has a code from the other machine.

   ```bash
   aya pair --code {CODE} --peer {remote_label} --as {local_instance}
   ```

   - `--code`: the pairing code from the other machine.
   - `--peer`: what to call the remote peer (e.g., "home" if pairing from work).
   - `--as`: this machine's local identity.

5. **Verify.** After pairing completes, run `aya status` to confirm the trusted key was added. Report success:

   ```
   Paired successfully.
   Trusted: {remote_label} ({remote DID prefix}...)
   ```

### Notes

- Pairing codes expire after 10 minutes.
- Both machines must have `aya init` completed before pairing.
- The `--peer` flag names the *remote* peer, not the local machine.
- If pairing fails with a timeout, suggest trying again — relay propagation can be slow.
- After pairing, suggest running `aya schedule install` if hooks aren't set up yet.

---

## 3. Status

Run a full aya readiness check and surface anything actionable.

### Steps

1. **Run status.**
   - **MCP (preferred):** `aya_status()` — returns the same structured
     payload directly, no TTY/format concerns.
   - **CLI fallback:**

     ```bash
     aya status -f json
     ```

2. **Parse and present.** Render in this order, skipping empty sections:

   #### Systems
   - Show a single line: `systems ok` or list any failing checks with their detail.

   #### Alerts (`alerts[]`)
   - List each alert as `{id[:8]} — {message}`.
   - If there are more than 5, show the first 5 and note how many more.
   - If none: skip this section.

   #### Watches (`watches[]`)
   - List each active watch: `{id[:8]} — {target} ({provider})`.
   - If none: skip this section.

   #### Due reminders (`due[]`)
   - List each: `{message}` — these need attention now.
   - If none: skip this section.

   #### Upcoming (`upcoming[]`)
   - List the next 3: `{message} — {due}`.
   - If none: skip this section.

   #### Perspective
   - Print the `perspective` field as a closing line (Ship Mind voice, no label).

3. **Offer next steps** if there are alerts:
   - "Want me to triage these alerts?" → run `/relay check`
   - "Want to dismiss any?" → `aya schedule dismiss {id}` (CLI-only; no
     MCP equivalent).

---

## 4. Refresh

Refresh is **CLI-only** — `uv tool` installer commands and
`aya schedule install` have no MCP equivalents. Reinstall aya, with
verification.

### Detect install source first

Before reinstalling, check whether the current install is editable from
a local source clone (common when developing aya itself):

```bash
cat ~/.local/share/uv/tools/aya-ai-assist/uv-receipt.toml 2>/dev/null
```

If the receipt has `editable = "/path/to/aya"` pointing at an existing
directory, run **the editable path** below — clobbering it with a
GitHub install kills "source edits are live without reinstall." If the
receipt is missing, points at a dead path, or has no `editable` flag,
run **the GitHub path**.

### Editable path (local source clone)

When the receipt points at a live local clone, let `<aya-clone>`
denote the path read from the receipt (e.g. `~/dev/code/aya`):

1. **Pull the clone up to date** (refresh = "get the latest"):

   ```bash
   git -C <aya-clone> status --porcelain
   git -C <aya-clone> pull --ff-only
   ```

   If `status --porcelain` shows uncommitted changes, stop and ask the
   user before pulling. Editable installs run against the working tree;
   pulling could surprise in-flight work.

2. **Reinstall editable** (re-syncs deps from `pyproject.toml`):

   ```bash
   uv tool install --editable <aya-clone> --reinstall
   ```

3. Continue at **Common steps** below.

### GitHub path (no local clone)

When the receipt is missing or non-editable:

1. **Uninstall current version:**

   ```bash
   uv tool uninstall aya-ai-assist
   ```

2. **Reinstall latest from GitHub:**

   ```bash
   uv tool install --from git+https://github.com/shawnoster/aya aya-ai-assist --force
   ```

3. Continue at **Common steps** below.

### Common steps (both paths)

4. **Re-install hooks** (picks up any format changes):

   ```bash
   aya schedule install
   ```

5. **Verify installation:**

   ```bash
   which aya && aya status -f json | jq '.systems.ok'
   ```

If the final command returns `true`, installation succeeded. If it returns `false` or errors, the installation failed — report the error to the user.

Do not continue if any step fails.

---

## 5. Watch

Add a watch on a GitHub PR (or other target) with sensible defaults.

### Steps

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

   `aya_schedule_watch` exists as an MCP tool but exposes only
   `provider`/`target`/`message` — it cannot set `--remove-when`, the
   poll interval, or a condition. Use MCP only for a minimal default
   watch; use CLI when the watch needs auto-remove, a custom interval,
   or a condition (the common case for GitHub PRs):

   - **MCP (minimal watch):** `aya_schedule_watch(provider="{provider}",
     target="{target}", message="{message}")`.
   - **CLI (preferred for PR watches with auto-remove + interval):**

     ```bash
     aya schedule watch {provider} {target} \
       -m "{message}" \
       --remove-when merged_or_closed \
       -i {interval}
     ```

6. **Confirm.** Report the watch ID and that it's active.

### Notes

- For PRs the user just opened or is reviewing, default to `--remove-when merged_or_closed` so the watch cleans itself up.
- If the user says "let me know when it's approved", add `--condition approved_or_merged`.
- Watch IDs support prefix matching for later dismiss/snooze operations.

---

## Cross-cutting rules

1. **Always resolve the local identity** before running aya commands that need `--as`. Run `aya status` and use the instance label from the output.

2. **Hand off between verbs** when the flow calls for it — Setup offers Pair at the end, Status can lead into triage. Don't make the user re-invoke.

3. **Don't duplicate relay skills.** Sending packets, checking the relay, and triaging inbox are handled by `/relay`. If the user asks to send something or check for messages, route to the relay skill instead.

---

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `aya` command not found | Not installed | `uv tool install aya-ai-assist` |
| `aya init` fails with "already initialized" | Identity exists | Skip to hooks/pairing |
| `aya pair` times out | Relay propagation lag | Retry; check both machines have connectivity |
| `aya status -f json` errors | Older aya version | Run verb 4 (Refresh) first |
| `gh` commands fail in Watch | gh CLI not authenticated | `gh auth login` |
