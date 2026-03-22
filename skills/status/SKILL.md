---
name: status
description: Workspace readiness check — identity, memory, integrations, and assistant health
---

# Status Check

Run a full readiness check for this workspace and report the result.

---

## 1. Run aya status

If `aya` is installed, run `aya status`. Report the output verbatim.

If aya is not installed, note that and continue with a manual check.

---

## 2. Workspace check

- Confirm the workspace root (look for `AGENTS.md` or `CLAUDE.md` at the root or in `assistant/`)
- Read `assistant/config.json` (or `config.json`) — verify `projects_dir` and `code_dirs` are set and exist
- Check `assistant/persona.md` — confirm persona is loaded
- Check `~/.copilot/assistant_profile.json` — confirm identity is present

---

## 3. Integration check

Report which integrations are currently connected:

| Integration | Status | Notes |
| ---- | ---- | ---- |
| Project tracking (Jira, Linear, etc.) | ✅ / ❌ | |
| Code hosting (GitHub, GitLab, etc.) | ✅ / ❌ | |
| Messaging (Slack, Teams, etc.) | ✅ / ❌ | |
| Calendar | ✅ / ❌ | |

---

## 4. Memory and scheduler check

- Read `assistant/memory/scheduler.json` — report count of active reminders and any that are due or overdue
- Check `assistant/memory/done-log.md` — confirm it exists (create if missing)

---

## 5. Report

Output a status summary:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Workspace Status

  Identity:     ✅ / ⚠️ MISSING
  Persona:      ✅ / ⚠️ MISSING
  Config:       ✅ / ⚠️ MISSING
  Scheduler:    N active reminders (N due)
  Integrations: N connected

  Overall: ONLINE / DEGRADED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

If DEGRADED, list the missing items and propose the smallest repair steps.

If ONLINE, confirm the assistant persona and memory links are active.

---

## Aliases

This skill runs for any of these requests:
- "status check"
- "bridge report"
- "readiness probe"
- "healthcheck"
- "loadout check"
- "assistant status"
