# ai-assist

**Personal AI assistant toolkit.**

`assist` is a CLI for managing your AI assistant across machines — sync context between instances, schedule reminders, and bootstrap new workspaces.

## Install

```bash
git clone https://github.com/shawnoster/helm.git
cd helm
uv sync
```

## Quick start

```bash
# Bootstrap a workspace
uv run assist bootstrap --root ~

# Set up identity
uv run assist init --label work

# Pair with another machine
uv run assist pair --label work        # shows a code
uv run assist pair --code WORD-WORD-0000 --label home  # on the other machine

# Send a packet
echo "Hello from work" | uv run assist pack --to home --intent "test" | uv run assist send /dev/stdin

# Check inbox
uv run assist inbox
```

## Commands

| Command | What it does |
| ---- | ---- |
| `assist init` | Generate identity keypair for this instance |
| `assist pair` | Pair two instances via short-lived relay code |
| `assist trust` | Manually trust a DID |
| `assist pack` | Create a signed knowledge packet |
| `assist send` | Publish a packet to a Nostr relay |
| `assist inbox` | List pending packets |
| `assist receive` | Review and ingest packets |

## How it works

- **Identity**: `did:key` (ed25519) for packet signing + secp256k1 for Nostr transport
- **Transport**: Nostr relays (NIP-01, kind 5999) — async, federated, self-hostable
- **Packets**: Signed JSON envelopes with markdown content, TTL, and conflict strategies
- **Security**: Signature verification, user approval before ingest, trust registry

## License

MIT
