# aya

**Personal AI assistant toolkit.**

`aya` is a CLI for managing your AI assistant across machines — sync context between instances, schedule reminders, and bootstrap new workspaces.

## Install

```bash
# From PyPI (after first release)
uvx aya

# From GitHub — works today, no PyPI required
uvx --from git+https://github.com/shawnoster/helm aya

# From source
git clone https://github.com/shawnoster/helm.git
cd helm
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
