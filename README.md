# aya

**Personal AI assistant toolkit.**

`aya` is a CLI tool that AI hosts call — sync context between instances, schedule reminders, and manage identity. The workspace (your guild repo) defines how your AI host behaves; `aya` is the tool it uses.

## Why "aya"?

Och, ye might well ask. It started life as `assistant-sync` — perfectly descriptive, perfectly dull, the kind o' name a committee'd be proud of. Then came `helm`, which sounded braw and nautical until some wee Kubernetes chart showed up and said *"Naw, that's mine."*

So there we were, rootin' around for a name, and someone muttered *"aya"* — and that was that. In the Scots tongue, *aya* is what ye say when somethin' lands just right. Not a grand *"YES"* mind ye, more a quiet *"aye, that'll do."* The kind o' sound a canny person makes when the kettle's found, the fire's lit, and everything's settled where it ought tae be.

That's this tool. Nae fuss. Nae ceremony. Just quietly doin' the job.

## Install

```bash
# From PyPI (after first release)
uvx aya

# From GitHub — works today, no PyPI required
uvx --from git+https://github.com/shawnoster/aya aya

# From source
git clone https://github.com/shawnoster/aya.git
cd aya
uv sync
# After sync, run commands with `uv run aya`, or install globally:
# uv tool install .
```

> **Python 3.14 note:** coincurve 21.0.0 does not ship a cp314 wheel, and
> building from the coincurve 21.0.0 sdist fails with cffi 2.0.0. Use
> Python 3.12 or 3.13 until this is resolved upstream. For example:
> `uvx --python 3.12 --from git+https://github.com/shawnoster/aya aya`

## Quick start

```bash
# Set up identity on each machine
aya init --label alice       # on Alice's machine
aya init --label bob         # on Bob's machine

# Pair them
aya pair --peer bob          # on Alice's machine — shows a code
aya pair --code WORD-WORD-0000 --peer alice   # on Bob's machine

# Send a packet
aya send --to bob --intent "build notes" --files notes.md

# Check inbox
aya inbox
```

Labels can be anything — `home`/`work`, names, machine hostnames. They're local aliases for the keypair on each side.

### Identity flags: `--as`, `--label`, `--peer`

Three flags name *who* you're talking about, and they're easy to confuse:

- **`--label <name>`** — used **only** with `aya init`, names *this* machine's local identity (e.g. `aya init --label alice` registers an instance called `alice` in the local profile).
- **`--as <name>`** — picks *which local identity* to act as for a command. Defaults to `default`; with multiple instances on a machine, pass `--as alice` to disambiguate.
- **`--peer <name>`** — names a *remote* identity (the one you've paired with). Used with `aya pair`, `aya trust`, etc.

A few older commands accept `--label` as a deprecated alias for `--peer`. Prefer `--peer` in new scripts. Quick mnemonic: `--label` *creates* a local name, `--as` *selects* one, `--peer` *targets* a remote one.

## Scheduling

aya has its own scheduler, but Claude Code also has a separate cloud-based automation system called CCR (Claude Code Remote). They solve different problems — knowing which to reach for saves a lot of friction.

### The one question

> *Does this task need me, or just need to be done?*

If you need to be informed or make a decision → **aya schedule**.
If it can be completed without you → **CCR remote trigger**.

### aya schedule — human in the loop

aya's scheduler runs on your machine. Alerts are delivered at session start via the `SessionStart` hook — they surface *to you* in your session context. You read them, decide, act.

**Use aya for:**

| What | Example |
| ---- | ---- |
| Reminders | `aya schedule remind -m "review PRs" --due "in 2 hours"` |
| Movement / wellness nudges | Recurring micro-prompts injected each session |
| Watch and tell me | "Watch PR #50 — alert me when it's approved" |
| Ticket state changes | "Tell me when JIRA-123 moves to In Review" |
| Anything requiring your judgment | CI is red, inbox has 10 packets, standup in 15min |

aya watches *poll* for state and produce *alerts*. You are the actor.

### CCR — human out of the loop

CCR (Claude Code Remote) runs isolated agents in Anthropic's cloud on a cron schedule. The agent clones your repo, does work, and exits — whether you're online or not. Use the `schedule` skill in Claude Code to create triggers.

**Use CCR for:**

| What | Example |
| ---- | ---- |
| PR feedback bot | Address review comments, push, reply to threads |
| Dependency updates | Weekly: open a PR bumping outdated packages |
| CI failure → ticket | Open a bug issue when main goes red |
| Stale PR cleanup | Comment on PRs idle >2 weeks |
| Nightly health report | Post a repo summary to Slack |
| Auto-merge | Merge approved PRs with passing checks |
| Release notes draft | Weekly: summarize merged PRs into a draft |
| Triage | Label and assign new issues by content |

CCR agents act *autonomously*. If you want to hear about what they did, wire them to Slack or Gmail via MCP connectors in the trigger.

### Choosing between them

```
Does it need my attention or judgment?
  Yes → aya schedule (watch / remind)

Can it be completed without me?
  Yes → CCR trigger

Does it need to run more often than hourly?
  Yes → aya schedule (any interval via system cron)

Does it need my local files or environment?
  Yes → aya schedule + SessionStart hook
```

CCR minimum interval is 1 hour. aya can fire at any cron interval.

---

## One-prompt setup

Already have aya installed on another machine? Give Claude Code this prompt on the new machine and it will handle everything:

> Install aya (`uv tool install aya-ai-assist`), initialize identity with `aya init --label <LABEL>`, then pair with my other instance. If I have a pairing code from the other machine, run `aya pair --code <CODE> --peer <OTHER_LABEL> --as <LABEL>` and we're done. If not, run `aya pair --peer <OTHER_LABEL> --as <LABEL>` — this will block waiting for the other machine to join (up to 10 minutes), so give me the short code it displays and immediately run `aya pair --code <CODE>` on the other machine before the window expires. After pairing, install hooks and crontab with `aya schedule install`. Finally, add the aya plugin to your shell profile: `alias claude='claude --plugin-dir /path/to/aya/.claude-plugin'` and verify everything with `aya status`.

Replace `<LABEL>` with a name for this machine (e.g. `home`, `work`, `laptop`, your name), `<OTHER_LABEL>` with the other machine's label, and `<CODE>` with the pairing code.

### What that prompt does

1. **Installs aya** globally via uv
2. **Creates identity** — generates ed25519 + secp256k1 keypairs
3. **Pairs instances** — exchanges trust via short-lived relay code
4. **Installs hooks** — `aya schedule install` wires Claude Code hooks and system crontab automatically
5. **Loads the plugin** — makes `/aya` and `/relay` skills available for managing aya and communicating between instances

---

## Agent integration (Claude Code)

aya is designed to surface alerts and reminders *into* your agent session, not just on the terminal. One command sets up everything:

```bash
aya schedule install          # crontab + Claude Code hooks
aya schedule install --dry-run  # preview first
```

This installs:
- A system crontab entry (`*/5 * * * *`) for background polling (watches, reminders, claim sweeping)
- Claude Code hooks for session activity tracking, cron registration, packet receiving, and CI monitoring

To remove everything: `aya schedule uninstall`.

### What the hooks do

| Hook | Event | What it does |
| ---- | ---- | ---- |
| `aya schedule activity` | SessionStart, PreToolUse | Resets the idle back-off timer so session crons aren't suppressed |
| `aya hook crons` | SessionStart | Reads pending session crons, injects `CronCreate` instructions into session context |
| `aya receive --quiet --auto-ingest` | SessionStart | Ingests packets from trusted senders in the background |
| `aya schedule pending --format text` | SessionStart | Prints due reminders and alerts directly into the session |
| `aya hook watch` | PostToolUse (Bash) | Polls all due scheduler watches and wakes agent on change (CI, PR, Jira) |

### How session crons work

`aya hook crons` is the bridge between aya's persistent scheduler and Claude Code's in-session cron system. On each session start it:

1. Fetches active session-required recurring items (without claiming alerts)
2. Filters by idle back-off and work-hours constraints
3. Outputs a `hookSpecificOutput.additionalContext` block with explicit `CronCreate` instructions

The agent reads those instructions and **must call `CronCreate` for each cron before responding**. This registers recurring jobs for the session — so a `*/15 * * * *` PR watch fires automatically every 15 minutes without the user having to ask.

### Registering a session cron

```bash
# Watch a PR — fires every 15 min, Mon–Fri
aya schedule recurring \
  --message "pr123-merge-watch" \
  --cron "*/15 * * * 1-5" \
  --prompt "Check PR #123. If merged, watch staging deploy and notify."
```

The cron is persisted in aya's scheduler store. On the next session start, `aya hook crons` picks it up and injects the `CronCreate` call automatically.

### PostToolUse: unified watch

After every shell command, aya polls all due scheduler watches (CI checks, GitHub PRs, Jira tickets) and wakes Claude if any condition fires:

```json
{
  "matcher": "Bash",
  "command": "aya hook watch 2>/dev/null || true",
  "asyncRewake": true
}
```

After a `git push`, aya monitors triggered GitHub Actions workflows and wakes the agent if a check fails.

### Non-Claude-Code agents

If your host doesn't support hooks, run this manually at the top of each session:

```bash
aya schedule pending --format text
```

This prints all due reminders, alerts, and session cron prompts as plain text. Copy any session cron prompts into your context to pick them up.

---

## Claude Code plugin

aya ships as a Claude Code plugin. Load it in dev mode by pointing the
`--plugin-dir` flag at the `.claude-plugin/` subdirectory of your local
clone:

```bash
alias claude='claude --plugin-dir /path/to/aya/.claude-plugin'
```

This loads aya's bundled skills:

| Skill | Verbs | What it does |
| ---- | ---- | ---- |
| `/aya` | setup, pair, status, refresh, watch | Manage aya — identity, pairing, health checks, updates, PR/ticket watches |
| `/relay` | check, read, reply, send, status | Relay communication — send/receive packets between instances with structured output and auto-polling |

After editing any skill file in the aya repo, run `/reload-plugins` in your session to pick up changes — no reinstall needed.

---

## Commands

| Command | What it does |
| ---- | ---- |
| `aya version` | Show the installed aya version |
| `aya init` | Generate identity keypair for this instance |
| `aya pair` | Pair two instances via short-lived relay code |
| `aya trust` | Manually trust a DID |
| `aya send` | Build, sign, and publish a knowledge packet to a Nostr relay |
| `aya send-raw` | Publish a pre-built packet file to a Nostr relay |
| `aya inbox` | List pending (un-ingested) packets |
| `aya receive` | Review and ingest packets from the relay |
| `aya read` | Read the body of a stored packet (`--meta` for headers, `--panel` for boxed display) |
| `aya ack` | Acknowledge a received packet (sends a reply back) |
| `aya drop` | Delete an ingested packet from local storage |
| `aya packets` | List stored packets, most recent first |
| `aya context` | Build a context block from workspace state |
| `aya status` | Workspace readiness check — systems, schedule, focus |
| `aya mcp-server` | Start the MCP server (stdio transport) for Claude Code |
| `aya schedule remind` | Add a one-shot reminder |
| `aya schedule watch` | Add a polling watch (GitHub PR, Jira ticket/query) |
| `aya schedule recurring` | Add a persistent recurring session job |
| `aya schedule activity` | Record user activity — resets the idle back-off timer |
| `aya schedule is-idle` | Check whether the session is currently idle |
| `aya schedule list` | List scheduled items |
| `aya schedule dismiss` | Dismiss a scheduled item or alert (prefix match OK) |
| `aya schedule snooze` | Snooze a reminder until a given time |
| `aya schedule alerts` | Show alerts from the background watcher |
| `aya schedule tick` | One scheduler cycle — poll watches, expire alerts (system cron uses this) |
| `aya schedule pending` | Show unclaimed alerts + session crons (SessionStart hook reads this) |
| `aya schedule install` | Install scheduler integrations — system crontab + Claude Code hooks |
| `aya schedule uninstall` | Remove scheduler integrations |
| `aya schedule status` | Scheduler overview — watches, reminders, deliveries |
| `aya relay list` | List configured relays |
| `aya relay add` | Add a relay to the default list |
| `aya relay remove` | Remove a relay from the default list |
| `aya relay status` | Show relay health and identity info |
| `aya config show` | Show the current workspace configuration |
| `aya config set` | Set a workspace configuration value |
| `aya log show` | Show daily notes |
| `aya log append` | Append to daily notes |
| `aya log auto` | Enable auto-logging of session notes |
| `aya hook crons` | (Internal — wired by `aya schedule install`) Convert active recurring schedules into Claude Code `CronCreate` instructions |
| `aya hook watch` | (Internal — wired by `aya schedule install`) Poll due watches and emit `asyncRewake` on change |

## How it works

- **Identity**: `did:key` (ed25519) for packet signing + secp256k1 for Nostr transport
- **Transport**: Nostr relays (NIP-01, kind 5999) — async, federated, self-hostable
- **Encryption**: NIP-44 v2 (secp256k1 ECDH + ChaCha20 + HMAC-SHA256) — on by default for public relays
- **Packets**: Signed JSON envelopes with markdown content, TTL, and conflict strategies
- **Security**: End-to-end encryption, signature verification, user approval before ingest, trust registry

## License

MIT
