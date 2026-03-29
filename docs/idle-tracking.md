# Idle Tracking & Session Crons — Architecture Review

## How It Works Today

### Data flow

```
SessionStart
  │
  ├── aya hook crons
  │     └── get_session_crons()
  │           ├── load recurring items from scheduler.json
  │           ├── filter: work hours (only_during)
  │           ├── filter: idle back-off (is_idle)
  │           └── emit CronCreate instructions for survivors
  │
  ├── aya schedule pending --format text
  │     └── get_pending()
  │           ├── claim alerts (atomic O_EXCL)
  │           └── get_session_crons() again (display only)
  │
  └── aya receive (async, unrelated)

During session
  │
  ├── PreToolUse hook → aya schedule activity
  │     └── record_activity() → writes ~/.aya/activity.json
  │           {"last_activity_at": "2026-03-29T14:30:00-06:00"}
  │
  └── Claude Code native cron engine fires registered crons
        └── aya has no visibility into whether they actually fire
```

### Idle determination

`is_idle(threshold)` checks: `(now - last_activity_at) >= threshold`

Special cases:
- No activity file / no `last_activity_at` → **not idle** (first-run safe)
- Empty threshold string → **never idle** (disables the feature)
- Exactly at threshold → **idle** (>= comparison)

### Activity file

- **Path:** `~/.aya/activity.json`
- **Scope:** Global — shared across all sessions and instances on this machine
- **Format:** `{"last_activity_at": "<ISO timestamp>"}`
- **Protection:** fcntl advisory lock + atomic rename

### Suppression priority

Work hours checked first, then idle. If outside work hours AND idle, reason
reported is "outside work hours" — idle check never runs.

---

## Design Gaps

### 1. Activity is global, not per-session

`activity.json` is a single file. If two sessions run concurrently, activity
from one resets the idle timer for both. This is fine for a single-user
single-machine setup (the current use case) but would break with true
multi-session use.

### 2. Cron registration is one-shot

`aya hook crons` runs at SessionStart. If crons are suppressed (idle or
work hours) at that moment, they stay suppressed for the entire session.
There's no mechanism to re-evaluate mid-session.

**Consequence:** If you start a session at 7:55am with `only_during: 08:00-18:00`,
those crons never register even though you're about to be in-window.

### 3. No feedback loop from Claude Code crons

Aya emits CronCreate instructions but has no way to know:
- Whether Claude Code actually created the cron
- Whether the cron fired
- Whether the user dismissed it
- Whether it survived a context compression

### 4. Idle ≠ absent

The idle timer measures "time since last tool call" (via PreToolUse hook).
But a user can be:
- Reading a long response (active, no tool calls)
- Thinking before their next message (active, no tool calls)
- AFK with a session open (truly idle)

All three look the same to the system.

### 5. First-run bootstrap problem

On a fresh session with no prior activity file, `is_idle()` returns `False`
(by design — prevents blocking first-run crons). But if the user starts a
session, walks away for an hour, and comes back to a new session, the stale
timestamp from the old session makes it look idle immediately.

The PreToolUse hook added today fixes the steady-state case. But the
SessionStart → hook_crons ordering matters: `aya hook crons` runs before
any PreToolUse fires, so suppression is evaluated against the *previous*
session's last activity.

**Fix:** Add `aya schedule activity` as the first SessionStart hook, before
`aya hook crons`, so session start itself counts as activity.

---

## Use Cases

These represent how a user actually wants scheduling to behave. Each case
should be testable end-to-end.

### UC-1: Health break reminder during active work

> "Remind me to stand up every 20 minutes while I'm working."

**Expected behavior:**
- Cron fires every 20 min during an active session
- If user walks away (no tool calls for 10+ min), reminders stop
- When user returns, reminders resume

**Current behavior:**
- Works IF the cron was registered at SessionStart (not suppressed)
- Idle back-off suppresses *registration*, not *firing*
- Once registered, Claude Code's cron engine fires it regardless of activity
- If suppressed at startup, never registers even after activity resumes

**Gap:** Idle back-off is a registration-time gate, not a runtime gate.
The cron fires into an empty room or never registers at all.

**Possible fix:** Move idle check into the cron prompt itself:
`"Run 'aya schedule is-idle --threshold 10m' — if active, remind user to
stand up. If idle, skip silently."`

### UC-2: Work-hours-only cron

> "Check my Jira board every hour, but only during work hours (8am-6pm)."

**Expected behavior:**
- Fires hourly between 8am and 6pm
- Never fires outside that window
- Works across session restarts within the window

**Current behavior:**
- If session starts within work hours → cron registers, fires every hour
  (including outside work hours, since Claude Code doesn't know about the
  window)
- If session starts outside work hours → cron never registers

**Gap:** `only_during` is a registration gate, not a per-fire check.
Same class of problem as UC-1.

### UC-3: Relay poll while session is alive

> "Check the relay for new packets every 30 minutes as long as I have a
> session open."

**Expected behavior:**
- Polls relay every 30 min
- Stops when session ends (natural — cron dies with session)
- Stops if user is AFK for 30+ min (no point polling if nobody reads it)

**Current behavior:**
- If registered, polls every 30 min regardless of activity
- Idle back-off only affects initial registration

**Gap:** Same registration-time-only suppression issue.

### UC-4: Start session after being away

> User closes laptop at 5pm. Opens it at 9am next day. Starts new Claude
> session.

**Expected behavior:**
- All crons register fresh
- Activity timer resets
- No "you were idle" suppression — this is a new session

**Current behavior:**
- `aya hook crons` runs → `is_idle("10m")` checks activity.json
- Last activity was 16+ hours ago → idle → crons suppressed
- Session starts with no crons despite being a brand-new session

**Fix (proposed above):** `aya schedule activity` as first SessionStart
hook resets the timer before `aya hook crons` evaluates it.

### UC-5: Long reading session

> User asks Claude to explain a complex architecture. Reads the response
> for 15 minutes without sending another message.

**Expected behavior:**
- Health break reminder fires at 20 min mark (user is still present)
- System doesn't think user left

**Current behavior:**
- PreToolUse hook doesn't fire (no tool calls during reading)
- After 10 min, `is_idle()` would return true
- But cron is already registered in Claude Code, so it fires anyway
- If a NEW session started right now, crons would be suppressed

**Observation:** The idle system doesn't affect already-registered crons.
This is actually fine for this case — but by accident, not by design.

### UC-6: Concurrent sessions

> User has two terminals open, both running Claude Code sessions.

**Expected behavior:**
- Both sessions get their crons registered
- Activity in either session keeps both "alive"

**Current behavior:**
- Both read/write the same `activity.json` — activity from either resets
  the timer (works by accident)
- Both try to claim alerts — first one wins (correct, by design)
- If one session is idle and the other is active, the active one's
  PreToolUse hook keeps the timer fresh for both

**Observation:** Global activity file happens to work here, but it's
fragile. If we ever move to per-session tracking, this breaks.

### UC-7: Cron that should fire once then stop

> "Remind me about the standup at 9:45am tomorrow."

**Expected behavior:**
- One-shot reminder, not a recurring cron
- Fires once, then goes away

**Current behavior:**
- This is a `schedule remind`, not a recurring cron
- Reminder → alert at due time → delivered at next session or tick
- Works correctly via the alert pipeline, not the cron system

**Observation:** Users might conflate reminders and crons. The system
correctly separates them, but the UX should make this clear.

### UC-8: Session spans work-hours boundary

> User starts session at 5:30pm. `only_during: 08:00-18:00`. Still
> working at 6:30pm.

**Expected behavior (option A):** Cron keeps firing — user is clearly active.
**Expected behavior (option B):** Cron stops at 6pm — respect the boundary.

**Current behavior:** Cron registered at 5:30pm (in-window). Claude Code
fires it at 6:30pm because CC doesn't know about `only_during`. Fires
indefinitely until session ends.

**Gap:** No runtime enforcement of work-hours window.

---

## Recommendations

### Short-term (fix the bootstrap)

1. **Add `aya schedule activity` as the first SessionStart hook** — ensures
   a new session always starts with a fresh activity timestamp, preventing
   stale-idle suppression. ✅ Done (PreToolUse hook added; SessionStart
   ordering still needed).

2. **Move `aya schedule activity` before `aya hook crons` in SessionStart**
   so the activity reset happens before cron evaluation.

### Medium-term (runtime idle checks)

3. **Embed idle checks in cron prompts** — instead of suppressing
   registration, register all crons but include `aya schedule is-idle`
   in the prompt so Claude can skip the action at fire-time.

4. **Add `only_during` enforcement to cron prompts** — same pattern:
   register the cron, but let the prompt check the time window before
   acting.

### Longer-term (architecture)

5. **Distinguish "session alive" from "user active"** — session start/end
   are lifecycle events; tool calls measure engagement. These are different
   signals and shouldn't share one timer.

6. **Cron heartbeat** — periodically verify registered crons still exist
   in Claude Code's cron list (if CC exposes this).

7. **Per-session activity tracking** — if multi-session becomes real,
   move to `activity.{session_id}.json` with a reaper for stale files.
