---
name: morning
description: >
  Adaptive morning briefing with priorities, calendar, open work, and a
  day plan. Invoke at session start, or when the user says "good morning",
  "what's on today", "start of day", "what did I miss", or "brief me".
  Depth adapts: full on weekday morning, focused on weekday afternoon,
  light on weekends.
argument-hint: "[quick]"
---

# Morning Briefing

Deliver a full delta briefing covering everything that changed since the last session, then produce a prioritized day plan.

Run all data-gathering steps (2–6) concurrently — do not wait for one to finish before starting the next.

---

## 0. Context detection (run first)

Note the current date and time. Determine:

- **Day type**: weekday (Mon–Fri) vs. weekend
- **Time of day**: morning (before noon) / afternoon / evening
- **Environment**: run `aya status` if available — use the output to understand what workspace, identity, and integrations are active. If aya is not installed, skip gracefully.

**Adapt depth based on context:**

| Context | Briefing depth |
| ---- | ---- |
| Weekday, morning | Full — all sections, all integrations |
| Weekday, afternoon/evening | Focused — skip calendar setup, surface blockers only |
| Weekend | Light — personal items only, skip work tracking |

---

## 1. Workspace readiness and carry-overs

Check for `assistant/notes/daily/{TODAY}.md`. If it exists and has a `## Carry-overs from {YESTERDAY}` section (written by last night's `/eod`), **treat those items as the top of Tier 1** — do not re-derive them from integrations. Preserve the slip reason note so context isn't lost:

```
Carrying from {YESTERDAY}: {item} — {slip reason, e.g. "blocked: waiting on review"}
```

If today's file already has a full `## Priority Stack` (i.e. `/morning` was already run today), summarize what's already in the plan rather than rebuilding from scratch.

Look for a `Makefile` with an `assistant-status` or `status-check` target in the workspace root. If present, run it and surface any DEGRADED state before proceeding.

Read `assistant/AGENTS.md` (or `AGENTS.md` at the workspace root) to identify active projects and their status files.

---

## 2. Project tracking — open work

If a project tracking integration is connected (Jira, Linear, GitHub Issues, or similar):

- Fetch tickets/issues assigned to the current user that are not Done/Closed
- For each item capture: identifier, summary, status, priority, last updated, due date (if set)
- Run a secondary query for items recently commented on or mentioned in

If no integration is available, scan `projects/*/status.md` files for open items and blockers.

---

## 3. Pull requests and code review

If a code hosting integration is connected (GitHub, GitLab, etc.):

**PRs needing my attention:**
- PRs where review is requested from me
- My open PRs with "changes requested"
- My PRs that are approved and ready to merge

For each PR: repo, number, title, status, last activity, review decision.

Flag: blocking someone else (review-requested) > my PRs needing updates > ready to merge.

---

## 4. Communication — messages and mentions

If a messaging integration is connected (Slack, Teams, etc.):

- Search for direct mentions in the last 3 days
- Surface threads I'm in with unread activity
- Check DMs that may need a response

Cluster as: needs-reply / FYI-only / thread-with-replies.

If no messaging integration is available, skip this section.

---

## 5. Calendar — today's schedule

If a calendar integration is connected:

- Pull today's events (time, title, attendees)
- Flag back-to-back blocks with no buffer
- Flag meetings that likely need prep (reviews, cross-team syncs, 1:1s with agenda)

If no calendar integration is available, skip this section.

---

## 6. Local project status

Read `status.md` for each active project listed in `assistant/AGENTS.md` (or `AGENTS.md`).

Flag:
- Projects with a listed blocker
- Projects with a deadline within 7 days
- `status.md` files not updated in the last 2 days (stale)

Also scan `assistant/notes/meetings/` and `projects/*/meetings/` for notes from the last 5 days. Extract open action items where the owner is the current user.

---

## 7. Synthesize — Morning Briefing

Produce the briefing in this structure. Omit sections that have no data.

```
### Morning Briefing — {YYYY-MM-DD}

**Delta**: {N} open tickets · {N} PRs need attention · {N} messages · {N} events today

---

#### Tier 1 — Act today
Items that are blocked, overdue, await my response, or have imminent deadlines.

| # | Item | Source | Why urgent |

---

#### Tier 2 — Advance today
In-flight work that can make meaningful progress.

| # | Item | Source | Context |

---

#### Tier 3 — On radar
Open and assigned, no immediate pressure.

| # | Item | Source | Notes |

---

#### PRs — status board

| PR | Repo | My role | Status | Action |

---

#### Today's calendar

Chronological list. Flag meetings needing prep.

---

#### Open action items from recent meetings

Bulleted list from meeting notes (last 5 days).

---

#### Stale project status files

List status.md files not updated recently.
```

---

## Prioritization logic (applied in order)

1. Anything blocking another person (review requested, awaiting my reply)
2. Hard deadlines within 7 days
3. My PRs with changes requested — unblocks others
4. Open meeting action items from the last 5 days
5. Tickets In Progress — maintain momentum
6. My PRs approved and mergeable — quick win
7. Tickets To Do with no deadline
8. Evergreen / strategic work

---

After delivering the briefing:

> "Anything missing? Want me to drill into a specific area?"

Save the briefing to `assistant/notes/daily/{YYYY-MM-DD}.md` if that file doesn't already exist. If carry-overs were present, note the count: `({N} carried from yesterday)`.
