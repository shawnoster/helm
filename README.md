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
```

## Quick start

```bash
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

## How it works

- **Identity**: `did:key` (ed25519) for packet signing + secp256k1 for Nostr transport
- **Transport**: Nostr relays (NIP-01, kind 5999) — async, federated, self-hostable
- **Packets**: Signed JSON envelopes with markdown content, TTL, and conflict strategies
- **Security**: Signature verification, user approval before ingest, trust registry

## License

MIT
