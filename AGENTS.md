# aya — Agent Guide

aya is a CLI tool that AI agents call to schedule reminders, sync context between machines, and integrate with Claude Code sessions. Agents never import aya as a library — they invoke it via shell commands.

## Quick Reference

### Scheduling

```bash
# One-shot reminder
aya schedule remind -m "Check the PR" --due "in 1 hour"

# Watch a GitHub PR (default polls every 5 min for PRs)
aya schedule watch github-pr owner/repo#123 -m "PR approved" --remove-when merged_or_closed

# Watch a Jira ticket
aya schedule watch jira-ticket CSD-225 -m "Ticket status changed"

# Recurring session cron (fires during active sessions only)
aya schedule recurring -m "health-break" -c "*/20 * * * *" \
  -p "Stand up, stretch, hydrate." --idle-back-off 10m

# Record user activity (resets idle timer)
aya schedule activity

# Check what's pending for this session
aya schedule pending --format json

# List active items
aya schedule list

# Dismiss or snooze
aya schedule dismiss <id-prefix>
aya schedule snooze <id-prefix> --until "in 1 hour"
```

### Dispatch / Relay

```bash
# Send context to another machine (encrypted by default on public relays)
aya dispatch --as alice --to bob \
  --intent "context sync" --files path/to/file.md

# Send a conversation seed (request for research/action)
aya dispatch --as alice --to bob --seed \
  --intent "investigate caching" \
  --opener "Can you trace the auth flow and find where sessions drop?"

# Send plaintext (debug or private relay only)
aya dispatch --as alice --to bob --no-encrypt --intent "test"

# Check inbox
aya inbox --as alice

# Receive and ingest trusted packets (decrypts transparently)
aya receive --as alice --auto-ingest --quiet

# Fully non-interactive receive — ingest everything without prompting (trusted or not)
aya receive --as alice --auto-ingest --yes --quiet

# Set up recurring relay poll (persists across sessions)
aya schedule recurring -m "relay-poll" -c "*/10 * * * *" \
  -p "Run: aya receive --as alice --auto-ingest --quiet. If any packets were ingested, surface their content to the user."
```

> **New machine?** See the "One-prompt setup" section in `README.md` for a single prompt that installs aya, pairs instances, wires hooks, and registers relay polling.

### Identity

```bash
# First-time setup — label can be anything (name, machine role, hostname)
aya init --label alice

# Pair with another machine (initiator)
aya pair --peer bob --as alice
# On the other machine (joiner)
aya pair --code WORD-WORD-1234 --peer alice --as bob

# Check status
aya status
```

> **`--as` vs `--label` vs `--peer`** — three flags, three roles:
> - `--as` is your **local identity** (which keypair to act as). Matches the label from `aya init --label <name>`. Legacy alias: `--instance`.
> - `--label` is used with `aya init` to **name a new local identity**. (In older versions, `--label` was also used where `--peer` is now; some commands still accept it as a legacy alias.)
> - `--peer` names a **remote machine** (used in `pair` and `trust`). Preferred over the legacy `--label` alias.
>
> Common label patterns: `home`/`work` (personal setup), first names (sharing with a friend), `laptop`/`desktop`/`server` (by machine).

## Plugin & Slash Commands

aya ships as a Claude Code plugin. Load it with:

```bash
claude --plugin-dir /path/to/aya
```

Or add a permanent alias to your shell profile:

```bash
alias claude='claude --plugin-dir /path/to/aya'
```

Available slash commands (work in any project):

| Command | What it does |
|---------|--------------|
| `/aya-send` | Pack and dispatch a packet to another machine |
| `/aya-triage-packets` | Receive and route incoming packets |
| `/aya-pair` | Guided pairing between two instances |
| `/aya-setup` | First-run bootstrap (identity, hooks, polling) |
| `/aya-watch` | Watch a GitHub PR with smart defaults |

After editing skill files, run `/reload-plugins` to pick up changes live.

## How Session Crons Work

aya persists recurring schedules. Claude Code fires them during sessions. The bridge:

1. `aya schedule recurring` stores the cron in `~/.aya/scheduler.json`
2. At session start, the `aya hook crons` command reads pending crons
3. It outputs `hookSpecificOutput` JSON telling Claude Code to call `CronCreate`
4. Claude Code's native cron system handles the timing from there

Idle back-off: crons with `--idle-back-off 10m` are suppressed if no activity for 10+ minutes. Call `aya schedule activity` from hooks to reset the timer.

Work hours: crons with `--only-during 08:00-18:00` only fire within that window.

## Watch Providers

| Provider | Target | Condition | Notes |
|----------|--------|-----------|-------|
| `github-pr` | `owner/repo#123` | `approved_or_merged` | Uses `gh` CLI. `--remove-when merged_or_closed` auto-cleans. |
| `jira-query` | JQL string | `new_results` | Requires `ATLASSIAN_EMAIL`, `ATLASSIAN_API_TOKEN`, `ATLASSIAN_SERVER_URL` env vars. |
| `jira-ticket` | `CSD-225` | `status_changed` | Same Jira env vars. |

## Packet Types

**Content packets** (default) carry knowledge — the receiver integrates it.

**Seed packets** (`--seed`) carry questions — the receiver investigates and reports back. Use `--opener` for the opening prompt.

Conflict strategies: `last_write_wins` (default), `surface_to_user`, `append`, `skip_if_newer`.

## Data Layout

All aya data lives under `~/.aya/`:

```
~/.aya/
  profile.json      # Identity, keypairs, trusted keys
  config.json       # Workflow config
  scheduler.json    # Reminders, watches, recurring crons
  alerts.json       # Unseen alerts from watchers
  activity.json     # Last activity timestamp (idle tracking)
```

## Claude Code Integration

### Quick setup

```bash
aya schedule install        # installs crontab + Claude Code hooks
aya schedule install --dry-run  # preview without changing anything
```

This installs the system crontab entry for background polling and all required
Claude Code hooks in `~/.claude/settings.json`. Run it once per machine.
To remove everything: `aya schedule uninstall`.

### Hooks installed

| Hook | Event | Purpose |
|------|-------|---------|
| `aya schedule activity` | SessionStart, PreToolUse | Resets idle back-off timer on session start and each tool call |
| `aya hook crons` | SessionStart | Converts aya's recurring schedules into Claude Code CronCreate calls |
| `aya receive` | SessionStart | Ingests packets from trusted senders in background |
| `aya schedule pending` | SessionStart | Surfaces due reminders and alerts into session context |
| `aya hook watch` | PostToolUse (Bash) | Polls all due scheduler watches and wakes agent on change (CI, PR, Jira) |

## Common Patterns

**After user says "remind me":**
```bash
aya schedule remind -m "Review the deploy" --due "tomorrow 9am"
```

**After opening a PR:**
```bash
aya schedule watch github-pr owner/repo#456 -m "PR review" --remove-when merged_or_closed
```

**Sending context to another machine:**
```bash
aya dispatch --as alice --to bob --seed \
  --intent "research request" \
  --opener "What logging do we have for the payment flow?"
```

**Checking scheduler health:**
```bash
aya schedule status
```

## Important Notes

- All `--format json` output uses `console.out()` to avoid Rich wrapping — safe to pipe.
- Item IDs support prefix matching: `aya schedule dismiss 5dc6` works if unambiguous.
- `aya schedule tick --quiet` is the system cron entry point (`*/5 * * * *`), installed via `aya schedule install`.
- Packets expire after 7 days by default.
- Trust is explicit — only paired/trusted DIDs are accepted.
