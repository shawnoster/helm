# Architecture

## System Overview

aya is a personal AI assistant toolkit that spans multiple machines. Each machine runs its own assistant instance with its own context, connected by signed async packets through Nostr relays.

```
┌─────────────────────────────────┐     ┌─────────────────────────────────┐
│         WORK MACHINE            │     │         HOME MACHINE            │
│                                 │     │                                 │
│  ┌───────────┐  ┌────────────┐  │     │  ┌───────────┐  ┌────────────┐ │
│  │ Claude    │  │ aya CLI   │  │     │  │ Claude    │  │ aya CLI   │ │
│  │ Code      │──│ + Plugin   │  │     │  │ Code      │──│ + Plugin   │ │
│  └─────┬─────┘  └─────┬──────┘  │     │  └─────┬─────┘  └─────┬──────┘ │
│        │              │         │     │        │              │        │
│  ┌─────┴──────────────┴──────┐  │     │  ┌─────┴──────────────┴──────┐ │
│  │      Workspace            │  │     │  │      Workspace            │ │
│  │  ~/guild/                 │  │     │  │  ~/                       │ │
│  │  ├── assistant/           │  │     │  │  ├── assistant/           │ │
│  │  │   ├── persona.md       │  │     │  │  │   ├── persona.md      │ │
│  │  │   └── memory/          │  │     │  │  │   └── memory/         │ │
│  │  ├── projects/            │  │     │  │  ├── projects/           │ │
│  │  ├── code/                │  │     │  │  ├── code/               │ │
│  │  └── scripts/             │  │     │  │  └── scripts/            │ │
│  └───────────────────────────┘  │     │  └───────────────────────────┘ │
│                                 │     │                                │
│  ┌───────────────────────────┐  │     │  ┌───────────────────────────┐ │
│  │ Profile                   │  │     │  │ Profile                   │ │
│  │ ~/.copilot/               │  │     │  │ ~/.copilot/              │ │
│  │   assistant_profile.json  │  │     │  │   assistant_profile.json │ │
│  │   ├── alias: "Ace"        │  │     │  │   ├── alias: "Ace"       │ │
│  │   ├── did:key (ed25519)   │  │     │  │   ├── did:key (ed25519)  │ │
│  │   ├── nostr (secp256k1)   │  │     │  │   ├── nostr (secp256k1)  │ │
│  │   └── trusted_keys: [home]│  │     │  │   └── trusted_keys:[work]│ │
│  └───────────────────────────┘  │     │  └───────────────────────────┘ │
│                                 │     │                                │
│  MCPs: Jira, Slack, GitHub,     │     │  MCPs: Google Calendar,       │
│  Confluence, Granola             │     │  personal tools               │
└──────────────┬──────────────────┘     └──────────────┬─────────────────┘
               │                                       │
               │         ┌───────────────┐             │
               │         │  Nostr Relay  │             │
               └────────►│  (nos.lol /   │◄────────────┘
                         │  relay.damus) │
                         │               │
                         │  kind: 5999   │
                         │  Signed JSON  │
                         │  packets      │
                         └───────────────┘
```

## Identity Model

Each instance has two keypairs serving different purposes:

```
┌─────────────────────────────────────────────────┐
│                   Identity                       │
│                                                  │
│  ┌─────────────────────┐  ┌──────────────────┐  │
│  │  did:key (ed25519)  │  │ Nostr (secp256k1)│  │
│  │                     │  │                  │  │
│  │  • W3C standard     │  │  • BIP-340       │  │
│  │  • Packet signing   │  │    Schnorr sigs  │  │
│  │  • Identity proof   │  │  • Relay         │  │
│  │  • Works offline    │  │    transport     │  │
│  │  • Zero infra       │  │  • Event routing │  │
│  └─────────────────────┘  └──────────────────┘  │
│                                                  │
│  Trust: explicit registry in profile             │
│  Pairing: relay-mediated short code exchange     │
└─────────────────────────────────────────────────┘
```

## Packet Lifecycle

```
SENDER (work)                    RELAY                    RECEIVER (home)
─────────────                    ─────                    ───────────────

1. User: "pack for home"
   │
2. Assistant gathers context
   │
3. aya pack
   ├── Create JSON envelope
   ├── Sign with ed25519
   └── Set TTL, intent, conflict strategy
   │
4. aya send
   ├── Wrap in Nostr event (kind 5999)
   ├── Sign with secp256k1 (Schnorr)
   └── Publish to relay ──────────────► 5. Relay stores event
                                           tagged with
                                           recipient pubkey
                                                │
                         6. SessionStart hook ◄──┘
                            aya receive --quiet
                            │
                         7. Query relay for
                            packets to my pubkey
                            │
                         8. Verify ed25519
                            signature
                            │
                         9. Surface to user
                            "1 packet from work"
                            │
                        10. User approves
                            │
                        11. Ingest into context
```

## Pairing Flow

```
MACHINE A                        RELAY                    MACHINE B
─────────                        ─────                    ─────────

1. aya pair --label work
   │
2. Generate code: ANCHOR-NORTH-0045
   │
3. Hash code (SHA-256)
   │
4. Publish pair-request ────────► 5. Stored with
   (kind 5999, tag: as-pair-req)     code hash tag
   │
6. Display code to user                              7. User enters code
   "ANCHOR-NORTH-0045"                                  aya pair --code
                                                        ANCHOR-NORTH-0045
                                                        --label home
                                                        │
                                 8. Query by ◄────── 9. Hash code, query
                                    code hash           for matching request
                                        │
                                        └──────────► 10. Found! Extract
                                                         work's DID + pubkey
                                                         │
                                 11. Stored ◄─────── 12. Publish pair-response
                                                         (kind 5999, tag:
                                                          as-pair-resp)
13. Poll finds response ◄───────
    │
14. Extract home's DID + pubkey
    │
15. Both instances now trust              16. Both instances now trust
    each other                                each other
```

## Adaptive Commands

The same skill produces different output based on available MCPs:

```
┌──────────────────────────────────────────────────────┐
│                 /aya:morning                         │
│                                                      │
│  1. Detect available data sources                    │
│     !`aya status --json`                            │
│                                                      │
│  2. Branch by what's available:                      │
│                                                      │
│  ┌─────────────────────┐  ┌────────────────────────┐ │
│  │  WORK (all MCPs)    │  │  HOME (minimal MCPs)   │ │
│  │                     │  │                        │ │
│  │  • Jira tickets     │  │  • Google Calendar     │ │
│  │  • GitHub PRs       │  │  • Helm inbox          │ │
│  │  • Slack mentions   │  │  • Project status      │ │
│  │  • Confluence       │  │  • Scheduler           │ │
│  │  • Granola          │  │                        │ │
│  │  • Helm inbox       │  │                        │ │
│  │  • Project status   │  │                        │ │
│  │  • Scheduler        │  │                        │ │
│  └─────────┬───────────┘  └───────────┬────────────┘ │
│            │                          │              │
│            ▼                          ▼              │
│  Full work briefing          Light personal briefing │
│  Tier 1/2/3, PRs,           Calendar, inbox,        │
│  Slack threads, prep         personal projects       │
└──────────────────────────────────────────────────────┘
```

## Bootstrap Flow

```
Fresh machine
     │
     ▼
git clone github.com/shawnoster/helm
cd helm && uv sync
     │
     ▼
aya bootstrap --root ~
     │
     ├── Create workspace skeleton
     │   ~/assistant/, ~/projects/, ~/code/, ~/scripts/
     │
     ├── Generate framework files
     │   CLAUDE.md, AGENTS.md, persona.md, config.json, Makefile
     │
     ├── Copy scripts
     │   scheduler.py, status_check.py, assistant_profile.py
     │
     └── Create dotfiles
         ~/.copilot/assistant_profile.json  (alias, persona, reminders)
         ~/.claude/settings.json            (hooks: health crons + aya receive)
         ~/.claude/hooks/health_crons.sh    (movement reminder injection)
     │
     ▼
aya init --label home
     │
     ▼
aya pair --code <CODE> --label home
     │
     ▼
Ready. Open Claude, packets auto-surface.
```

## Component Map

```
aya (CLI + Plugin)
├── Identity
│   ├── identity.py      — did:key gen, ed25519 + secp256k1 keypairs
│   └── Profile          — load/save assistant_profile.json
│
├── Sync
│   ├── packet.py        — JSON envelope, signing, verification
│   ├── relay.py         — Nostr WebSocket client (kind 5999)
│   └── pair.py          — short-code pairing via relay
│
├── Schedule
│   └── scheduler.py     — reminders, watches, recurring jobs, polling
│
├── Status
│   └── status.py        — workspace readiness check, daily notes parsing
│
├── Workspace
│   └── workspace.py     — bootstrap scaffolding, dotfile setup
│
└── CLI
    └── cli.py           — typer app wiring all subcommands
```

## Security Model

```
┌─────────────────────────────────────────────┐
│              Security Layers                 │
│                                              │
│  1. Signature verification                   │
│     └── Packet from known did:key?           │
│                                              │
│  2. Trust registry                           │
│     └── DID in trusted_keys list?            │
│                                              │
│  3. User approval                            │
│     └── Always surface before ingest         │
│         (unless --auto-ingest from trusted)   │
│                                              │
│  4. Role separation                          │
│     └── Packet content = user role context   │
│         Never system instructions            │
│                                              │
│  5. Optional encryption                      │
│     └── NaCl box to recipient pubkey         │
│         Relay sees size + TTL only           │
│                                              │
│  6. TTL / expiration                         │
│     └── Packets expire (default 7 days)      │
│         Relay discards after expiration      │
└─────────────────────────────────────────────┘
```
