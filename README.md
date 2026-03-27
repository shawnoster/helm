# aya

**Personal AI assistant toolkit.**

`aya` is a CLI for managing your AI assistant across machines — sync context between instances, schedule reminders, and bootstrap new workspaces.

## Why "aya"?

Och, ye might well ask. It started life as `assistant-sync` — perfectly descriptive, perfectly dull, the kind o' name a committee'd be proud of. Then came `helm`, which sounded braw and nautical until some wee Kubernetes chart showed up and said *"Naw, that's mine."*

So there we were, rootin' around for a name, and someone muttered *"aya"* — and that was that. In the Scots tongue, *aya* is what ye say when somethin' lands just right. Not a grand *"YES"* mind ye, more a quiet *"aye, that'll do."* The kind o' sound a canny person makes when the kettle's found, the fire's lit, and everything's settled where it ought tae be.

That's this tool. Nae fuss. Nae ceremony. Just quietly doin' the job.

## Skills

`aya bootstrap` installs a set of AI-agnostic skills into your workspace. Any harness that reads `skills/*/SKILL.md` or `.claude/commands/*.md` can invoke them — Claude Code, Copilot, OpenCode, Windsurf, or any future tool.

### Workflow cycle

```
STATUS ──────────────────► MORNING
                              │
               ┌──────────────┴────────────────────┐
               │                                   │
            FEATURE                          (open queue)
         (ticket → branch)
               │
          DISCOVERY → ARCHITECTURE → PLAN → IMPLEMENT
                                                │
                                             FINISH
                                  (commit · push · PR · ticket)
                                                │
                     ◄──────────────────── PIVOT ◄─── MEETING
                                    (tidy · signals · next)    (stages your
                                            │                 action items)
                                      keep working
                                            │
                                          EOD
                                  (reconcile · stage tomorrow)
                                            │
                                         MORNING ◄──────────────┘
                                      (reads carry-overs)
```

### Skill reference

| Skill | When to use |
| ---- | ---- |
| `/morning` | Start of day — briefing, priorities, calendar |
| `/eod` | End of day — reconcile plan, stage tomorrow |
| `/status` | Workspace readiness check |
| `/feature` | Start a new feature (ticket → branch) |
| `/pivot` | Between tasks — tidy up, scan signals, suggest what's next |
| `/finish` | Close out work — commit, push, PR, ticket, log |
| `/discovery` | Find relevant code for a project |
| `/architecture` | Understand how an existing system works |
| `/plan` | Design an implementation approach |
| `/implement` | Execute a plan and make code changes |
| `/meeting` | Capture meeting notes |

Skills are AI-agnostic — no hardcoded tool names, just plain English descriptions. Any harness can pick up the right skill from conversational context: "what's next" → `/pivot`, "how does X work" → `/architecture`, "let's ship this" → `/finish`.

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
```

## Quick start

```bash
# Bootstrap a workspace
uv run aya bootstrap --root ~

# Set up identity
uv run aya init --label work

# Pair with another machine
uv run aya pair --label work        # shows a code
uv run aya pair --code WORD-WORD-0000 --label home  # on the other machine

# Send a packet
echo "Hello from work" | uv run aya pack --to home --intent "test" | uv run aya send /dev/stdin

# Check inbox
uv run aya inbox
```

## Commands

| Command | What it does |
| ---- | ---- |
| `aya bootstrap` | Scaffold a workspace — config, skills, hooks, dotfiles |
| `aya reset` | Remove bootstrap files, keep persona and user data |
| `aya init` | Generate identity keypair for this instance |
| `aya profile` | Initialize or rotate the persistent assistant profile |
| `aya pair` | Pair two instances via short-lived relay code |
| `aya trust` | Manually trust a DID |
| `aya pack` | Create a signed knowledge packet |
| `aya send` | Publish a packet to a Nostr relay |
| `aya dispatch` | Pack + send in one step (no temp file) |
| `aya inbox` | List pending packets |
| `aya receive` | Review and ingest packets |
| `aya status` | Workspace readiness check — systems, schedule, focus |
| `aya ci` | CI integration — watch checks, report failures |
| `aya schedule remind` | Add a one-shot reminder |
| `aya schedule watch` | Add a polling watch (GitHub PR, Jira ticket/query) |
| `aya schedule recurring` | Add a persistent recurring session job |
| `aya schedule activity` | Record user activity — resets the idle back-off timer |
| `aya schedule is-idle` | Check whether the session is currently idle |
| `aya schedule list` | List scheduled items |
| `aya schedule check` | Check for due reminders and alerts |
| `aya schedule dismiss` | Dismiss a scheduled item or alert |
| `aya schedule snooze` | Snooze a reminder |
| `aya schedule alerts` | Show alerts from background watcher |
| `aya schedule tick` | One scheduler cycle — poll watches, expire alerts |
| `aya schedule pending` | Show unclaimed alerts + session crons (SessionStart hook) |
| `aya schedule status` | Scheduler overview — watches, reminders, deliveries |

## Bootstrap

`aya bootstrap` scaffolds a personal assistant workspace. Run it once on a new machine, re-run safely anytime — it's fully idempotent.

```bash
aya bootstrap --root ~/guild
```

### What gets created

```
~/guild/
├── CLAUDE.md                     # Root config — links to assistant docs
├── AGENTS.md                     # Workspace structure overview
├── Makefile                      # Convenience targets for scheduler
├── assistant/
│   ├── AGENTS.md                 # Duplicate for harness auto-discovery
│   ├── CLAUDE.md                 # Behavioral instructions
│   ├── persona.md                # Ship's Mind voice and tone
│   ├── profile.json              # Persona alias, movement reminders (canonical)
│   ├── config.json               # Workspace paths (projects_dir, code_dirs)
│   ├── memory/
│   │   ├── scheduler.json        # Persistent reminders, watches, crons
│   │   └── README.md             # Memory hub docs
│   ├── notes/                    # daily/, meetings/, ideas/
│   ├── templates/                # Project file templates
│   └── rules/                    # Repo conventions
├── projects/                     # Per-project status, plans, discovery
├── code/                         # Cloned code repositories
├── scripts/                      # Framework scripts (scheduler, status)
├── .claude/commands/             # Skills in legacy Claude Code format
└── skills/                       # Skills in harness-agnostic SKILL.md format
```

### Dotfiles

Bootstrap also sets up user-level config:

| File | Purpose |
| ---- | ---- |
| `~/.copilot/assistant_profile.json` | Symlink → `assistant/profile.json` (backward compat) |
| `~/.claude/settings.json` | SessionStart hooks (health crons, packet receive, scheduler pending) |
| `~/.claude/hooks/health_crons.sh` | Registers micro-nudge and stand-and-walk cron jobs |

The profile lives in the workspace (`assistant/profile.json`) — not in a harness-specific dotfile. The `~/.copilot/` symlink exists for tools that still look there.

Existing dotfiles are merged, not overwritten. Hooks are deduplicated on re-run.

### Idempotency

- Existing files are skipped (your edits are preserved)
- Existing hooks are not duplicated
- Running `aya bootstrap` twice is a no-op

## Reset

`aya reset` removes everything bootstrap created, keeping your work:

```bash
aya reset --root ~/guild
```

### Preserved on reset

- `assistant/persona.md` — your customized persona
- `assistant/profile.json` — your alias, movement reminders, identity
- `assistant/memory/scheduler.json` — your reminders and watches
- `assistant/memory/alerts.json` — unseen watcher alerts
- `assistant/memory/done-log.md` — completed work log
- `projects/` — all project status, plans, meeting notes, discovery docs
- Dotfiles (`~/.copilot/`, `~/.claude/`) — not touched by reset

### Bootstrap-reset cycle

```
bootstrap → customize → reset → bootstrap
```

Your persona and data survive the cycle. Config files are regenerated fresh.

## How it works

- **Identity**: `did:key` (ed25519) for packet signing + secp256k1 for Nostr transport
- **Transport**: Nostr relays (NIP-01, kind 5999) — async, federated, self-hostable
- **Packets**: Signed JSON envelopes with markdown content, TTL, and conflict strategies
- **Security**: Signature verification, user approval before ingest, trust registry

## License

MIT
