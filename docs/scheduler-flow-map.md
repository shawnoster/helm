# Scheduler Flow Map: systemd → aya → Claude Code

## Core Design: Ephemeral Session Crons

Session-required crons (health nudges, relay polls, etc.) intentionally skip execution when Claude Code is closed. This is **correct behavior**, not a bug.

**Why?** These crons need a listening host—Claude Code session context. Without an active session, there's nowhere to deliver the result. Next session start, the SessionStart hook re-registers them fresh via `aya hook crons`.

---

## Architecture Overview

### Two-Tier Execution Model

```
System crontab (every 5 min)
    ↓
aya schedule tick --quiet
    ↓
Checks: Which jobs are due?
    ├─ Session-required jobs (health nudges, relay polls)
    │  └─ IF Claude session active → enqueue as "pending" for session
    │  └─ ELSE → skip cleanly (no host to listen)
    │
    └─ Session-independent jobs (watches, reminders, webhooks)
       └─ Execute immediately regardless of session state

Claude Code SessionStart hook
    ↓
aya hook crons
    ↓
Re-registers all session crons fresh from scheduler state
```

### Integration Points

1. **System crontab entry** (installed by `aya schedule install`)
   ```
   */5 * * * * /home/shawn/.local/bin/aya schedule tick --quiet
   ```
   - Runs every 5 minutes
   - Checks what's due, executes, then exits
   - Not a long-running daemon

2. **Claude Code hooks** (installed by `aya schedule install`)
   - SessionStart: registers pending crons
   - PreToolUse: marks activity
   - PostToolUse: watches CI status

---

## Execution Paths

### When Claude Session is Active (Path A)

```
13:43 UTC - System cron ticks
    ↓
aya schedule tick checks: health-nudge due + session active?
    → YES
    ↓
Enqueue as "pending cron" in shared scheduler state
    ↓
Claude session scheduler picks up pending cron
    ↓
Evaluates prompt: "Deliver a single micro-nudge..."
    ↓
Result appears in conversation ✅
```

### When Claude Session is Closed (Path B)

```
13:43 UTC - System cron ticks
    ↓
aya schedule tick checks: health-nudge due + session active?
    → NO (session_required=true, no Claude session)
    ↓
Skip execution cleanly (no host to listen)
    ✓ Expected behavior
    ↓
Next session start: SessionStart hook re-registers crons fresh
```

---

## Failure Modes & Mitigations

### Timing & Execution

| Mode | Risk | Mitigation |
|------|------|-----------|
| **Race condition:** Multiple system cron ticks overlap | Duplicate job execution | Use `flock` in crontab to prevent overlapping runs |
| **Relay poll timing:** Every 10 min, but cron ticks every 5 min | Jitter in execution | Acceptable; expected behavior |

**Implementation:**
```bash
*/5 * * * * flock -n /var/run/aya-tick.lock /home/shawn/.local/bin/aya schedule tick --quiet
```

---

### Persistence & Environment

| Mode | Risk | Mitigation |
|------|------|-----------|
| **Session startup lag:** Alert fires at 13:43, session doesn't start until 14:15 | Alert data lost if aya crashes | Ensure scheduler uses persistent storage (SQLite/JSON file) |
| **Environment variables:** System crontab has minimal env | `$PATH` / `$HOME` missing in cron context | Use full paths in crontab entry (e.g., `/home/shawn/.local/bin/aya`) |

---

### Integration with Session State

| Mode | Risk | Mitigation |
|------|------|-----------|
| **Idle backoff:** Relay poll has `idle_back_off: 30m` | System cron might execute relay poll despite backoff | Verify aya's backoff logic respects system cron ticks (not just session checks) |
| **Logging gaps:** System cron execution is silent (no session output) | Job failures go unnoticed | Configure crontab to log: `>> ~/.aya/scheduler.log 2>&1` |

---

## Setup Checklist

To activate the scheduler with system crontab persistence:

- [ ] Run `aya schedule install` (sets up crontab + hooks)
- [ ] Verify: `crontab -l | grep "aya schedule tick"`
- [ ] Optional: Add logging to crontab entry for debugging
- [ ] Test: Close Claude session → wait 5 min (system cron tick) → reopen → verify crons re-register

---

## Success Criteria

For the integrated aya-native scheduler to work reliably:

1. ✅ `aya schedule install` sets up system crontab (every 5 min tick)
2. ✅ Session-required crons skip cleanly when no Claude session exists
3. ✅ SessionStart hook re-registers crons via `aya hook crons`
4. ✅ Session-independent jobs execute reliably via system cron
5. ✅ No duplicate executions (flock prevents overlap)
6. ✅ Environment variables available (full paths in crontab entry)
7. ✅ Idle backoff logic respected across system cron + session hooks
