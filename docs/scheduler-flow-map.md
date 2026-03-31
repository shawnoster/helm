# Full Flow Map: systemd → aya → Claude Code

## Current Setup (Session-Scoped Crons)

### Architecture
```
Claude Code Session starts
    ↓
SessionStart hook runs
    ↓
CronCreate registers 3 ephemeral crons in session memory
    (health nudges, relay poll)
    ↓
Session scheduler checks every ~1 sec (when session is idle)
    ↓
Cron fires → Prompt evaluated in session context
    ↓
Result appears in conversation
    ↓
Claude Code exits → Crons are lost
```

### Status
- ✅ Works reliably (prompt executes in active session context)
- ❌ Dies on session exit
- ❌ Prompts lost between sessions
- ❌ No execution history/logging
- ❌ Requires Claude Code to stay open

---

## Proposed Setup (systemd + aya daemon)

### Architecture (HIGH LEVEL)

```
system crontab (every 5 min)
    ↓
aya schedule tick --quiet
    ↓
aya daemon checks due jobs (watches, reminders, session crons)
    ↓
IF session_required=true AND no Claude session exists
    → Queue job for next session (via relay/alerts)
    ↓
IF session_required=false OR Claude session is active
    → Execute prompt immediately
    ↓
Result delivery (depends on job type)
```

### Key Discovery: `aya schedule install`

The `aya schedule install` command sets up:

1. **System crontab entry** (runs every 5 min)
   ```
   */5 * * * * /home/shawn/.local/bin/aya schedule tick --quiet
   ```
   - This is NOT a long-running daemon
   - It's a periodic task that wakes up, checks what's due, executes, then exits
   - More reliable than keeping a daemon alive

2. **Claude Code hooks** (already installed)
   - SessionStart
   - PreToolUse  
   - PostToolUse

### Execution Model: `aya schedule tick`

What happens when `aya schedule tick` runs:

1. Reads scheduler database (watches, reminders, crons)
2. Checks which items are due
3. **For session-required crons:**
   - If Claude Code session is active → Enqueue as "pending" cron for that session
   - If no session → Store as alert/reminder for next session
4. **For session-independent watches/reminders:**
   - Execute immediately (deliver email, trigger webhook, etc.)
5. Exit

The session picks up pending crons via the SessionStart hook.

---

## Detailed Flow Diagram: Health Nudge Execution Path

### Path A: During Active Claude Session (Current behavior, still works)

```
[Timeline] 13:43 UTC
    ↓
System crontab fires (every 5 min)
    ↓
aya schedule tick runs
    ↓
Checks: Is health-nudge cron due?
  YES (13:43 matches "13,43 * * * *")
    ↓
Checks: session_required=true AND is a Claude session active?
  YES (Claude Code is open)
    ↓
Enqueue as "pending cron" in shared scheduler state
    ↓
Claude Code session scheduler wakes up
    ↓
Reads pending crons from shared state
    ↓
Evaluates prompt: "Deliver a single micro-nudge..."
    ↓
Prompt runs in session context (access to memory, MCP, etc.)
    ↓
Result appears in conversation
    ✅ Success
```

### Path B: Claude Session Not Running (Currently fails)

```
[Timeline] 13:43 UTC
System crontab fires (every 5 min)
    ↓
aya schedule tick runs
    ↓
Checks: Is health-nudge cron due?
  YES
    ↓
Checks: session_required=true AND is a Claude session active?
  NO (Claude Code is closed)
    ↓
❓ QUESTION: What does aya do here?
   Option 1: Store as alert/reminder for next session start
   Option 2: Silently drop the job
   Option 3: Try to spawn a new Claude Code session
   Option 4: Queue it in relay as a packet
    ↓
??? (depends on aya implementation)
```

---

## Failure Modes & Edge Cases

### 1. **Session-Required Crons Skip When No Session Exists (Intended Behavior)**

**Scenario:** Claude Code is closed. System cron fires at 13:43. Nudge is `session_required=true`.

**What happens:** aya checks if a Claude session exists. If not, the cron is skipped (not executed, not queued, not dropped).

**Why this is correct:**
- Health nudges require a human at the keyboard to receive them
- Without an active Claude session, there's no host to listen
- Queueing them for later is unnecessary complexity
- Next session start, `aya hook crons` re-registers them

**No mitigation needed.** This is the intended design. Session-required crons are ephemeral by nature—they live for the duration of the Claude Code session, then die when it exits. On next session start, they're re-registered fresh.

---

### 2. **Relay Poll Misses Work Context**

**Scenario:** Relay poll is scheduled for `*/10 * * * *` (every 10 min), but system cron only ticks every 5 min.

**Timing risk:** Relay poll fires at 10, 20, 30 min marks. System cron ticks at 0, 5, 10, 15, 20, 25, 30 min.
- 10:00: tick fires, relay poll executes ✅
- 10:05: tick fires, relay poll not due ✓
- 10:10: tick fires, relay poll executes ✅

**Mitigation:** OK as-is. Jitter is expected.

---

### 3. **Race Condition: Multiple System Cron Ticks**

**Scenario:** System cron takes >5 min to complete. Next cron starts before first finishes.

**Risk:** Duplicate job execution (nudge fires twice).

**Current status:** `aya schedule tick --quiet` typically <1 sec, so unlikely. But if aya scheduler becomes more complex...

**Mitigation:** Add `flock` to crontab entry to prevent overlapping executions:
```
*/5 * * * * flock -n /var/run/aya-tick.lock /home/shawn/.local/bin/aya schedule tick --quiet
```

---

### 4. **systemd Timer vs System Crontab**

**Why we're NOT using systemd timers for aya:**

Current plan: Keep systemd for *other* health tasks, use aya's built-in system crontab for scheduler.

**Decision point:**
- ✅ Use `aya schedule install` (creates system crontab) for aya scheduler ticks
- ✅ Use systemd timers for *non-session* tasks (e.g., notebook backups, system maintenance)
- ❌ Don't duplicate scheduling—one source of truth per task

---

### 5. **Session Startup Lag**

**Scenario:** Health nudge fires at 13:43, stored as alert. Claude session doesn't start until 14:15.

**Question:** Does alert persist?

**Current:** `recent_deliveries` in `aya schedule status` shows deliveries up to 2026-03-30. Suggests alerts are logged.

**Risk:** If alerts aren't persisted to disk, they're lost if aya crashes before session starts.

**Mitigation:** Ensure aya scheduler is configured with persistent storage (SQLite, JSON file, etc.).

---

### 6. **Environment Variable Loss in System Crontab**

**Scenario:** Health nudge prompt references `$HOME` or `$PATH`.

**Risk:** System crontab has minimal environment. Variables may be missing.

**Example failure:**
```bash
# Prompt tries to run:
aya receive --instance home --quiet

# But $PATH might not include /home/shawn/.local/bin
# → aya: command not found
```

**Mitigation:** 
- Use full paths in crontab entry: `/home/shawn/.local/bin/aya schedule tick`
- Source `.bashrc` in prompt if needed: `source ~/.bashrc && <command>`

---

### 7. **Idle Backoff Interaction with System Cron**

**Current:** Relay poll has `idle_back_off: 30m`. Meaning: Don't run relay poll if session has been idle <30m.

**Risk:** System cron might execute relay poll even though idle backoff should suppress it.

**Flow:**
```
13:45 UTC - Claude session goes idle
13:50 UTC - System cron ticks, calls aya schedule tick
          - Relay poll is due (*/10 min)
          - But idle_back_off says "don't run for 30m"
          - ✅ aya should check backoff and skip
14:00 UTC - If backoff works correctly, relay poll skipped
14:15 UTC - Idle backoff expires, relay poll executes next tick ✅
```

**Mitigation:** Verify aya's `idle_back_off` logic respects system cron ticks (not just session cron checks).

---

### 8. **Logging & Observability Gap**

**Current state:**
- Session crons: Output appears in conversation (visible)
- System cron ticks: Log goes where? (invisible)

**Risk:** Job failures silent. No audit trail.

**Mitigation:**
- Configure aya to log all scheduler ticks to file: `~/.aya/scheduler.log`
- Set up crontab to capture output: `*/5 * * * * ... >> ~/.aya/scheduler.log 2>&1`
- Periodically read log in session: `/brief` skill could scan it

---

## Success Criteria (Option 2 Implementation)

For "aya-native + systemd persistence" to work (health nudges remain `session_required: true`):

1. ✅ `aya schedule install` sets up system crontab (runs `aya schedule tick` every 5 min)
2. ✅ Session-required crons skip cleanly when no Claude session exists
3. ✅ SessionStart hook re-registers crons via `aya hook crons`
4. ✅ Relay poll continues to work with idle backoff
5. ✅ Watches (non-session jobs) execute reliably via system cron
6. ✅ No duplicate executions (use flock if needed)
7. ✅ Environment variables available (full paths in cron entry)

---

## Action Items

To implement Option 2 (aya-native + systemd persistence):

1. **Run `aya schedule install`**
   - Adds system crontab entry: `*/5 * * * * /home/shawn/.local/bin/aya schedule tick --quiet`
   - Verifies SessionStart hooks are in place

2. **Add logging to crontab entry** (optional but recommended)
   - Configure crontab to capture output: `>> ~/.aya/scheduler.log 2>&1`
   - Allows debugging of system cron execution outside sessions

3. **Verify SessionStart hook flow**
   - Confirm `aya hook crons` runs on session start
   - Confirm `aya schedule pending` picks up alerts from watches
   - Check that relay poll respects idle backoff

4. **Test the loop**
   - Close Claude, wait for system cron tick (every 5 min)
   - Open Claude session
   - Verify health nudges register and fire normally
   - Verify relay polling continues (if applicable)

---

## Implementation Complexity

| Item | Complexity | Blocker? |
|------|-----------|----------|
| Run `aya schedule install` | Trivial (1 cmd) | No |
| Verify system cron ticks every 5 min | Trivial (check crontab) | No |
| Add logging to crontab (optional) | Trivial (1 line) | No |
| Test SessionStart re-registration | Low (manual test) | No |
| Prevent duplicate ticks (flock) | Low (1 line) | No |
