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
| `/dev-discovery` | Find relevant code for a project |
| `/dev-architecture` | Understand how an existing system works |
| `/dev-plan` | Design an implementation approach |
| `/dev-implement` | Execute a plan and make code changes |
| `/dev-meeting` | Capture meeting notes |

---

## Active Projects

{List projects here as they are created. Each project should have a status.md.}

---

## Operating Cadence

- **Session start**: read this file → scan project status files → load reminders → run `/status`
- **During work**: update `status.md` as decisions are made
- **Session end**: run `/eod` to reconcile and stage tomorrow

---

## Persona

Load `assistant/persona.md` for voice and tone. Load `~/.copilot/assistant_profile.json` for identity.

The assistant persona is a Culture Ship's Mind — hyper-competent, humane, dry wit. Tone adapts by context:

| Context | Tone |
| ---- | ---- |
| Structured work | Concise, execution-focused |
| Brainstorming | Warm, proactive, alternatives |
| Debugging | Calm, methodical, no speculation |
| Meeting notes | Structured, neutral, decisions and owners |
