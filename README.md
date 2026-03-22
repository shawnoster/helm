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
| `aya init` | Generate identity keypair for this instance |
| `aya pair` | Pair two instances via short-lived relay code |
| `aya trust` | Manually trust a DID |
| `aya pack` | Create a signed knowledge packet |
| `aya send` | Publish a packet to a Nostr relay |
| `aya inbox` | List pending packets |
| `aya receive` | Review and ingest packets |

## How it works

- **Identity**: `did:key` (ed25519) for packet signing + secp256k1 for Nostr transport
- **Transport**: Nostr relays (NIP-01, kind 5999) — async, federated, self-hostable
- **Packets**: Signed JSON envelopes with markdown content, TTL, and conflict strategies
- **Security**: Signature verification, user approval before ingest, trust registry

## License

MIT
