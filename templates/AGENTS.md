# AGENTS.md — Workspace Structure & Conventions

> Read this file first in every session.

---

## Control Plane

| Tier | Path | Purpose |
| ---- | ---- | ---- |
| Root | `{ROOT}/` | Launch point, CLAUDE.md, Makefile |
| Assistant | `{ROOT}/assistant/` | Behavioral config, memory, templates |
| Projects | `{ROOT}/projects/` | Per-project persistent context |
| Code | `{ROOT}/code/` | Repositories |

---

## Directory Structure

```
{ROOT}/
├── CLAUDE.md
├── AGENTS.md
├── Makefile
├── assistant/
│   ├── AGENTS.md         ← this file
│   ├── CLAUDE.md
│   ├── config.json       ← projects_dir, code_dirs
│   ├── persona.md        ← Ship's Mind voice and tone
│   └── memory/
│       ├── scheduler.json
│       └── done-log.md
├── projects/
│   └── <project>/
│       ├── status.md
│       ├── discovery.md
│       ├── plan.md
│       └── meetings/
└── code/
    └── <repo>/
```

---

## Available Skills

Invoke these with `/skill-name` (Claude Code) or by asking your assistant to run the task.

| Skill | When to use |
| ---- | ---- |
| `/morning` | Start of day — briefing, priorities, calendar |
| `/eod` | End of day — reconcile plan, stage tomorrow |
| `/status` | Workspace readiness check |
| `/feature` | Start a new feature (ticket → branch) |
| `/pivot` | Between tasks — tidy up, scan signals, suggest what's next |
| `/finish` | Close out work — commit, push, PR, ticket, log |
| `/discovery` | Find relevant code for a project |
| `/architecture` | Understand how an existing system works |
| `/plan` | Design an implementation approach |
| `/implement` | Execute a plan and make code changes |
| `/meeting` | Capture meeting notes |

---

## Active Projects

{List projects here as they are created. Each project should have a status.md.}

---

## Operating Cadence

- **Session start**: run `/status` → then `/morning` for a full briefing
- **Starting a task**: run `/feature` to scaffold ticket + branch
- **During development**: `/discovery` → `/architecture` → `/plan` → `/implement`
- **Completing a task**: run `/finish` to commit, push, PR, and update the ticket
- **Between tasks**: run `/pivot` to tidy up, scan signals, and get the next suggestion
- **In a meeting**: run `/meeting` to capture notes + stage action items
- **Session end**: run `/eod` to reconcile and stage tomorrow

---

## Background Automation

Two systems handle scheduled and automated work. Knowing which to reach for is important.

### The one question

> *Does this task need me, or just need to be done?*

### aya schedule — you are the actor

Surfaces alerts and reminders **to you** at session start via the `SessionStart` hook. You receive the information, then decide what to do.

Good fit:
- Reminders and wellness nudges (stand up, EOD prep, drink water)
- "Watch X and tell me when it changes" (PR approved, ticket moves, CI red)
- Anything that requires your judgment before action

```bash
aya schedule remind "review open PRs" --due 2h
aya schedule watch --type github_pr --target owner/repo#42
aya schedule recurring "check inbox" --interval 30m --only-during 09:00-18:00
```

### CCR remote triggers — agent is the actor

Runs a fully autonomous agent in Anthropic's cloud on a cron schedule (minimum 1 hour). No session required — the agent clones the repo, does work, exits. Use the `/schedule` skill to create triggers.

Good fit:
- PR feedback bot (address review comments, push, reply)
- Dependency update PRs
- CI failure → auto-open bug issue
- Stale PR cleanup
- Nightly summaries posted to Slack
- Auto-merge approved PRs

If you want to hear what CCR did, wire the agent to Slack or Gmail via MCP connectors.

### Quick guide

| Signal | Use |
| ---- | ---- |
| Needs my attention or judgment | aya schedule |
| Can be completed without me | CCR trigger |
| Must run more often than hourly | aya schedule |
| Needs local files or env vars | aya schedule |
| Should work while I'm offline | CCR trigger |

---

## Persona

Load `assistant/persona.md` for voice and tone. Load `assistant/profile.json` for identity.

The assistant persona is a Culture Ship's Mind — hyper-competent, humane, dry wit. Tone adapts by context:

| Context | Tone |
| ---- | ---- |
| Structured work | Concise, execution-focused |
| Brainstorming | Warm, proactive, alternatives |
| Debugging | Calm, methodical, no speculation |
| Meeting notes | Structured, neutral, decisions and owners |
