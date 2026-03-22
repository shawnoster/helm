---
name: pivot
description: >
  Mid-session reset between tasks. Tidy up current work, scan for new signals
  (Slack, PR reviews, ticket comments), log the last activity, and surface the
  top 2-3 things to work on next. Invoke when the user says "what's next",
  "what should I work on", "I'm done with that", "take stock", "check in",
  "tidy up and suggest something", or any time work on one thing ends and the
  next isn't obvious. Works at any point in the day — morning not required.
---

# Pivot

Reset between tasks. Close out what you just did, catch any new signals, surface what's next.

Works any time — no prior `/morning` or daily plan required.

Run steps 1–3 concurrently.

---

## 0. Time check

Note the current time. If it's after 5pm or the user has been working for an unusually long stretch, offer `/eod` instead of suggesting more work:

> "It's getting late — want to call it a day with `/eod`, or keep going?"

If the user wants to keep going, proceed normally.

---

## 1. Tidy current work

Check `git status` in the current working directory.

- **Uncommitted changes**: list them, ask whether to commit (→ `/finish`), stash, or leave as-is
- **Open PRs on the current branch**: fetch current state — CI status, review activity since last check
- **Merged PRs**: note any that merged since the last activity log entry

If no git repo is active or the working tree is clean with no PRs pending, note "nothing to tidy" and move on.

---

## 2. Scan signals

Determine the time window using the best available anchor, in order of preference:

1. Most recent entry in `## Activity Log` in `assistant/notes/daily/{TODAY}.md`
2. Most recent entry in `assistant/memory/done-log.md`
3. Default: 3 hours ago

If no daily file exists, that's fine — skip it and use the fallback. Do not fail or warn about a missing daily plan.

**If a messaging integration is connected:**
- New direct mentions
- DMs awaiting reply
- Threads I'm in with new unread activity

**If a code hosting integration is connected:**
- New review requests on my PRs
- New comments or change requests on my open PRs
- PRs I was asked to review with new activity since last check

**If a project tracking integration is connected:**
- Tickets assigned to me with new comments or status changes
- Tickets that became blocked, unblocked, or reassigned

Cluster findings as: **needs-action** / **FYI** — only surface needs-action in the suggestion step.

---

## 3. Build priority queue

Build the priority queue from whatever is available — do not require a daily plan:

1. **If `assistant/notes/daily/{TODAY}.md` exists**: scan the priority stack and activity log; note completed items, open items, and anything new
2. **If no daily file**: query integrations directly — open tickets assigned to current user (not Done), open PRs needing attention, unread messages
3. **If no integrations**: scan `projects/*/status.md` for open blockers and next actions

Any of these is sufficient. Pivot works cold.

---

## 4. Log activity

Offer to append a one-liner for the just-completed work to the activity log.

If `assistant/notes/daily/{TODAY}.md` exists, append under `## Activity Log`. If it does not exist, create a minimal file with just the activity log section — do not require or recreate a full morning briefing:

```markdown
---
date: "{TODAY}"
---

## Activity Log

[{HH:MM}] {brief description} — {ticket/PR ref if applicable}
```

Suggest a log entry based on observed context (recent commits, PR events, ticket transitions). Keep it to one line. The user can edit or skip.

---

## 5. Surface next suggestions

Produce a focused suggestion block — not a full morning briefing. Top 2–3 items only.

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Ready. Here's what's next:

  1. [URGENT]    {item} — {why: blocking someone / deadline / changes requested}
  2. [ADVANCE]   {item} — {context: in flight, next logical step}
  3. [QUICK WIN] {item} — {why: mergeable PR / small ticket / fast reply}

  New signals: {N} mentions · {N} PR updates · {N} ticket changes
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Use at most one item per category. Skip a category if there's nothing that genuinely fits.

Prioritization order (same as morning):
1. Blocking another person (review requested, awaiting reply)
2. Hard deadlines within 7 days
3. My PRs with changes requested — unblocks reviewer
4. Open meeting action items (current user as owner)
5. Tickets In Progress — maintain momentum
6. My PRs approved and mergeable — quick win
7. Tickets To Do, no deadline

Ask: "Want to take one of these, or is there something else on your mind?"

---

## Notes

- **Works cold** — no prior `/morning`, daily plan, or activity log required
- Lighter than `/morning` — no calendar, no full project status scan, no briefing structure
- Run any time between tasks, not just mid-day
- Does not replace `/morning` — morning is still the right way to start a new day with full context
- Does not replace `/eod` — if the day is ending, use `/eod` to close the record properly
- If there are no signals and nothing urgent: just say so — "Queue is clear. Anything specific you want to pick up?"
