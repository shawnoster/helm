---
name: eod
description: >
  End-of-day wrap — reconcile planned vs. actual, stage carry-overs, update
  done-log, write tomorrow's stub. Invoke when the user says "wrap up",
  "end of day", "close out today", "I'm done for the day", or "let's
  capture what got done".
---

# End of Day

Close the loop on today. Reconcile plan vs. reality, stage tomorrow, commit the record.

Run data-gathering steps (1–3) concurrently.

---

## 1. Load today's plan

Determine today's date. Read `assistant/notes/daily/{TODAY}.md`.

Extract:
- **Priority stack** — numbered/checked list at the top
- **Activity log** — entries under `## Activity Log`
- **Open PRs and tickets tables** — if present
- **Watch items** — if present

If no daily file exists for today, note that and proceed with an empty plan.

---

## 2. Check PR state

If a code hosting integration is connected, fetch current state for any PRs mentioned in today's plan or activity log.

For each PR: status (open / merged / closed / draft), review state, CI status, last activity.

---

## 3. Check ticket state

If a project tracking integration is connected, query for tickets assigned to the current user that are not Done.

For tickets that appeared in today's plan: current status, any transitions made today.

---

## 4. Reconcile — planned vs. actual

Map each item from the Priority stack against evidence from Steps 1–3.

**Status codes:**
- `✅ Done` — completed (merged PR, ticket moved to Done, activity log confirms)
- `🔄 In flight` — meaningful progress, not closed
- `➡️ Carry` — no observable progress; slips to tomorrow
- `❌ Blocked` — stuck on external dependency
- `➕ Unplanned` — happened but wasn't in the plan

**Infer slip reasons from evidence — do not ask:**
- Ticket moved to "Waiting on..." → "blocked: external"
- PR no new activity → "no reviewer / no time"
- Activity log shows different item → "displaced by [item]"
- Item absent from activity log → "not started"

---

## 5. Produce EOD report

```
### EOD Wrap — {TODAY}

**Summary**: {N} planned · {N} done · {N} in flight · {N} slipped · {N} unplanned

---

#### Planned vs. Actual

| # | Item | Planned | Actual | Note |

---

#### Unplanned work completed

Items in the activity log that weren't in the priority stack.

---

#### Active watches

Scheduled watches still running. Flag any that are stale or should be cancelled.

---

#### Blockers holding overnight

Who is the external dependency? Is there an action to take before logging off?
```

---

## 6. Stage tomorrow

Determine tomorrow's date. Skip weekends — if today is Friday, target Monday.

Check if `assistant/notes/daily/{TOMORROW}.md` exists.

- **Does not exist**: create it:

```markdown
---
date: "{TOMORROW}"
carry_overs_from: "{TODAY}"
---

# Daily Plan — {TOMORROW}

## Carry-overs from {TODAY}

<!--
These items slipped from yesterday. Morning will pick these up automatically
and place them at the top of Tier 1 — do not delete this section.
-->

{For each ➡️ Carry and 🔄 In flight item:}
- [ ] {item description} — {ticket/PR ref} *(slip: {reason, e.g. "blocked: awaiting review", "displaced by X", "not started"})*

## Priority Stack

(build from carry-overs + anything new)

---

## Activity Log
```

- **Already exists**: append a `## Carry-overs from {TODAY}` section with the same format. Do not overwrite existing content.

**Always include the slip reason** — even if inferred. Mark inferred reasons with `(inferred)`. Morning reads this note to give context without re-deriving from scratch.

Output: `✓ Tomorrow's stub → assistant/notes/daily/{TOMORROW}.md`

---

## 7. Update done-log

Append to `assistant/memory/done-log.md`:

```markdown
### {TODAY}
- {item description} — {ticket/PR reference}
```

Only log `✅ Done` and `🔄 In flight` items (partial credit). Skip carries and blocked items.

---

## 8. Confirm and close

Present the EOD report (Step 5). Ask:

> "Anything to add before I commit the record? Any slip reasons wrong, or work I missed?"

Wait for a response. Incorporate any corrections.

Then write the carry-over stub and done-log if not already done. Output:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Day closed.

  Done today:              N items
  Carrying to {TOMORROW}:  N items
  Blocked/external:        N items

  Tomorrow's stub → assistant/notes/daily/{TOMORROW}.md
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Notes

- Never ask "what did you do today" — infer from the activity log and observable state
- Slip reasons are best-effort; mark as `(inferred)` if not confirmed
- Skip weekends when staging tomorrow's stub
- If today has no daily notes file, produce an empty reconciliation and ask if there's work to log
