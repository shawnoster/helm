# aya Documentation

This folder contains architecture and operational guides for aya.

## Documents

### [architecture.md](./architecture.md)
**What it covers:** System design, identity model, packet lifecycle

- How aya spans multiple machines (home, work, servers, etc.)
- Identity layer: did:key (signing) + Nostr keys (relay transport)
- How packets flow: creation → signing → encryption → Nostr relay → decryption → verification
- High-level system overview

**Read this when:** You're understanding aya's core design or explaining it to others.

---

### [idle-tracking.md](./idle-tracking.md)
**What it covers:** Activity tracking, session cron suppression logic, design gaps

- How aya determines if a session is idle (global activity.json + last_activity_at)
- How session crons are suppressed during idle periods or outside work hours
- How the SessionStart hook emits CronCreate instructions filtered by idle/work-hours rules
- Known limitations (global activity, one-shot registration, no feedback from Claude)

**Read this when:** You're configuring `idle_back_off` on crons or understanding why a cron didn't fire.

---

### [scheduler-flow-map.md](./scheduler-flow-map.md)
**What it covers:** Job execution flow, system cron integration, failure modes

- End-to-end flow: system crontab → `aya schedule tick` → job execution
- Difference between session-required crons (ephemeral) and other jobs (persistent)
- Why session-required crons skip when Claude isn't running (by design)
- How system crontab tick complements Claude's session-scoped scheduler
- 8 potential failure modes and mitigations (race conditions, env vars, idle backoff interaction, etc.)

**Read this when:** You're debugging why a job didn't run, implementing `aya schedule install`, or understanding the split between system and session scheduling.

---

### [self-hosted-relay.md](./self-hosted-relay.md)
**What it covers:** Running a private Nostr relay on Synology NAS

- Why self-host (reliability, latency, privacy, control)
- Relay options (nostr-rs-relay vs strfry)
- Step-by-step setup on Synology with Docker
- Configuration (auth, limits, retention)
- Running multiple relays (failover, sync)

**Read this when:** You want to run aya packet sync locally instead of relying on public Nostr relays.

---

## Navigation

**New to aya?** Start with [architecture.md](./architecture.md).

**Configuring crons?** Read [idle-tracking.md](./idle-tracking.md) then [scheduler-flow-map.md](./scheduler-flow-map.md).

**Debugging a scheduler issue?** Check [scheduler-flow-map.md](./scheduler-flow-map.md) for failure modes and debugging steps.

**Self-hosting packet sync?** Follow [self-hosted-relay.md](./self-hosted-relay.md).
