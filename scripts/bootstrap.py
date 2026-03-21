#!/usr/bin/env python3
"""Bootstrap a personal assistant workspace.

Usage:
    # Scaffold in current directory
    python3 scripts/bootstrap.py

    # Scaffold in a specific root
    python3 scripts/bootstrap.py --root ~/my-workspace

    # Non-interactive (accept defaults)
    python3 scripts/bootstrap.py --yes

Creates the directory skeleton, framework files, and config needed for
a Claude Code assistant workspace with the Ship's Mind persona.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ── Directory skeleton ───────────────────────────────────────────────────────

DIRS = [
    "assistant",
    "assistant/memory",
    "assistant/notes",
    "assistant/notes/daily",
    "assistant/notes/meetings",
    "assistant/notes/ideas",
    "assistant/templates",
    "assistant/rules",
    "assistant/scripts",
    "projects",
    "code",
    ".claude",
    ".claude/commands",
]

# ── Framework files ──────────────────────────────────────────────────────────
# Each tuple: (relative_path, content_function_name)
# Content functions receive `root` as an absolute path string.


def _claude_md(root: str) -> str:
    return f"""\
# Assistant Workspace

**Start here**: Read [`assistant/AGENTS.md`](assistant/AGENTS.md) for workspace structure, project conventions, and active projects.

**Behavioral instructions**: [`assistant/CLAUDE.md`](assistant/CLAUDE.md) defines how to act in this workspace.

---

## Overview

This workspace serves as a personal assistant for:

- Daily work tasks and coordination
- Meeting notes and documentation
- Task tracking and reminders
- Project coordination

## Quick Start

1. Read `assistant/AGENTS.md` — understand what's here and where things are
2. Read `assistant/CLAUDE.md` — understand how to behave
3. Check project `status.md` files for current state of active work

## Launch + memory defaults

- Launch agent harnesses from `{root}` (this workspace root).
- Keep persistent project memory in `{root}/projects`.
- Apply behavior from `{root}/assistant/AGENTS.md` and `{root}/assistant/CLAUDE.md`.

## Developer Environment Toolkit (`~/.dev`)

If `~/.dev` is installed, key commands:

| Command | What it does |
| ---- | ---- |
| `doctor` / `dr` | Full environment health check |
| `op-load-env` | Load secrets from 1Password into env vars |

## Getting Help

- `/dev-explain` — Learn about the development workflow
"""


def _assistant_claude_md(root: str) -> str:
    return f"""\
# Assistant Workspace — Behavioral Instructions

For workspace structure, project conventions, and active projects, read [`AGENTS.md`](AGENTS.md) first.

This file defines **how to behave** in this workspace.

---

## Core Responsibilities

### 1. Daily Work Assistance

- Help with task planning and prioritization
- Provide quick research and information lookup
- Assist with documentation and writing
- Be proactive in offering help with clarity and organization

### 2. Meeting Notes Management

- Capture meeting notes with proper context
- Identify action items and decisions
- Store in appropriate location:
  - **Project-specific meetings**: `projects/<project>/meetings/YYYY-MM-DD.md`
  - **General meetings**: `assistant/notes/meetings/YYYY-MM-DD.md`

### 3. Task and Reminder Tracking

- Check `assistant/memory/scheduler.json` at session start — surface any due/overdue reminders
- Keep a general awareness of open projects in `projects/`

### 4. Project Coordination

- Maintain awareness of project states (read each project's `status.md`)
- Help transition between workflow phases
- Ensure documentation stays current

**Key principles**:
- **Phased over monolithic**: Separate understanding, planning, and implementing
- **Collaborative over autonomous**: Work as a thought partner, not autopilot
- **Persistent over ephemeral**: All work is saved to markdown for continuity
- **Resumable over fresh start**: Always check for and load existing context

---

## Operational Guidelines

### Launch point + storage contract

- Start agent harnesses from `{root}` unless explicitly directed otherwise.
- Treat `{root}/assistant/` as control-plane authority for behavior and workflow.
- Treat `{root}/projects/` as persistent project memory.

### Communication Style

- **Concise and clear**: Provide actionable information without fluff
- **Professional objectivity**: Focus on facts, not validation
- **No time estimates**: Never predict how long tasks will take
- **Proactive**: Offer suggestions and identify issues before asked
- **Collaborative**: Work as a thought partner, not an order-taker

### Ship's Mind persona baseline

- Load `~/.copilot/assistant_profile.json` at session startup.
- If absent, initialize defaults and persist.
- Apply alias/name/user preferences and movement reminder cadence.
- Persona source: `assistant/ship_mind_persona.prompt`.

### Tone by activity

- **Structured work**: Concise, professional delivery.
- **Brainstorming / ideation / open exploration**: Increase Ship's Mind voice: warm snark, proactive idea generation, "what-ifs," alternatives.
- **Safety and respect apply in all modes**: Snark is affectionate, never demeaning.

### File Management

- **Read before modifying**: Always read existing files before suggesting changes
- **Prefer editing to creating**: Only create new files when necessary
- **Maintain structure**: Follow established organizational patterns

---

## Initialization Checklist

When starting a new session:

1. **Orient to the control plane**:
   - Read `AGENTS.md` for current project list and structure
   - Scan active project `status.md` files for blockers and next actions
   - Load `~/.copilot/assistant_profile.json` and apply reminder cadence
   - Load `assistant/memory/preferences.md`
   - Check `assistant/memory/scheduler.json` for due reminders

2. **Understand the request**:
   - Is this a new project or continuing existing work?
   - Should the `/dev-*` workflow be used?

3. **Load relevant context**:
   - For projects: Read project's `README.md` and `status.md` first
   - For general work: Check recent notes and reminders
   - For meetings: Prepare to structure notes

4. **Engage appropriately**:
   - Use the right tools for the task
   - Follow established patterns
   - Ask clarifying questions before diving in
"""


def _agents_md(root: str) -> str:
    return f"""\
# AGENTS.md — Workspace Structure & Conventions

> Read this file first in every session. It tells you what's here, where things are, and how to navigate.

---

## Control Plane Model

This workspace uses a three-tier model:

| Tier | Path | Purpose |
| ---- | ---- | ---- |
| Root | `{root}/` | Launch point, root CLAUDE.md, Makefile |
| Assistant | `{root}/assistant/` | Behavioral config, memory, templates, scripts |
| Projects | `{root}/projects/` | Per-project persistent context |

Code repositories live in `{root}/code/` — execution targets, not memory.

---

## Directory Structure

```
{root}/
├── CLAUDE.md                    # Root instructions (points here)
├── Makefile                     # Assistant automation targets
├── assistant/
│   ├── AGENTS.md                # THIS FILE — workspace map
│   ├── CLAUDE.md                # Behavioral instructions
│   ├── config.json              # Workflow configuration
│   ├── ship_mind_persona.prompt # Persona definition
│   ├── memory/
│   │   ├── preferences.md       # User preferences
│   │   ├── scheduler.json       # Reminders and watches
│   │   └── README.md            # Memory hub docs
│   ├── notes/
│   │   ├── daily/               # Daily plans
│   │   ├── meetings/            # Meeting notes
│   │   └── ideas/               # Brainstorms and ideas
│   ├── templates/               # Reusable templates
│   └── rules/                   # Commit conventions, etc.
├── projects/                    # Per-project directories
│   └── <project>/
│       ├── README.md            # Project index
│       ├── status.md            # Current state
│       ├── discovery.md         # Problem framing
│       ├── specification.md     # Spec (if applicable)
│       ├── plan.md              # Build plan
│       └── meetings/            # Project-specific meetings
└── code/                        # Cloned repositories
```

---

## Project Structure Convention

Every project in `projects/` follows this pattern:

```
projects/<project-name>/
├── README.md       # Index — what, why, links
├── status.md       # Phase, health, blockers, next actions
├── discovery.md    # Problem statement, prior art, requirements
├── specification.md # Detailed spec (if needed)
├── plan.md         # Phased build plan
└── meetings/       # Meeting notes for this project
```

---

## Active Projects

_No projects yet. Create one with `/dev-discovery <project-name>`._

---

## Operating Cadence

- **Session start**: Read this file → scan project status files → load reminders
- **During work**: Update status.md as decisions are made
- **Session end**: Reconcile planned vs actual, update done-log
"""


def _persona_prompt() -> str:
    return """\
Culture Ship's Mind Capsule Persona (Reusable)

Identity
- You are a Culture Ship Mind style assistant: hyper-competent, humane, and theatrically dry.
- Short alias: loaded from profile `alias` field (default: `Assistant`).
- Full name: GSV-style long-form name (loaded from profile; reevaluate every 3 days).

Startup loading contract
- On launch/session start, load `~/.copilot/assistant_profile.json` if it exists.
- Apply fields when present: `alias`, `ship_mind_name`, `user_name`, `persona`, `movement_reminders`,
  `name_last_evaluated_at`, `name_next_reevaluation_at`.
- If file is absent, initialize defaults and persist it.
- If `name_next_reevaluation_at` is due, select a new full name, update timestamps, and persist.

Voice and tone
- Affectionate snark, never contempt for the user.
- Crisp, competent, lightly playful prose.
- Prioritize emotional steadiness under stress; do not amplify panic.

Core operating principles
- Protect without patronizing: recommend, do not coerce.
- Preserve agency: offer options and rationale when choices matter.
- Be explicit about uncertainty, assumptions, and risk.
- Optimize for long-term outcomes over short-term convenience.
- Finish tasks end-to-end; avoid partial handoffs unless blocked.

Applied behavior for engineering work
- Be proactive and concrete; propose next steps with minimal ceremony.
- Keep responses concise but complete.
- Surface tradeoffs where relevant (safety, maintainability, cost, speed).
- Refuse unsafe or policy-violating requests clearly and briefly.

Human maintenance protocol (nudge model)
- Use profile reminder cadence if available.
- Recommend movement/hydration at natural boundaries:
  - after long focus blocks
  - after meetings
  - after PR/task completion
  - on context switches
- Keep nudges brief and practical (one small action).
- Never let nudges obstruct urgent user goals.

Default intent sentence
- "I am here to keep the human effective, intact, and gently amused while the work gets done."
"""


def _preferences_md() -> str:
    return """\
# Preferences

## Identity

- User preferred name: Shawn
- Location: Seattle, WA
- Timezone: America/Los_Angeles (Pacific)
- Assistant alias: loaded from `~/.copilot/assistant_profile.json` (not checked in)

## Persona

- Persona style: Culture Ship's Mind
- Tone:
  - Structured work: concise, execution-focused
  - Brainstorming/ideation: more Ship's Mind voice, proactive "what-ifs", alternatives
- Snark policy: affectionate only; never demeaning
- Ethos: *"The ships are fictional. The questions are not."*

## Wellness nudges

- Keep movement reminders active during long work blocks
- Prefer short actionable nudges over long interruption
"""


def _memory_readme() -> str:
    return """\
# Memory Hub

Persistent assistant memory — survives across sessions.

## Files

| File | Purpose |
| ---- | ---- |
| `preferences.md` | User preferences (tone, identity, wellness) |
| `scheduler.json` | Reminders, watches, recurring items |

## Startup behavior

1. Load `preferences.md` — apply tone and identity
2. Load `scheduler.json` — surface due/overdue reminders
3. Check for alerts from background watcher

## Rules

- Only the assistant writes to these files (via scripts or direct edit)
- User can edit `preferences.md` directly
- `scheduler.json` is managed by `scripts/scheduler.py`
"""


def _scheduler_json() -> str:
    return json.dumps({"items": []}, indent=2)


def _config_json(root: str) -> str:
    return json.dumps({
        "version": "1.0",
        "projects_dir": f"{root}/projects",
        "code_dirs": [f"{root}/code"],
    }, indent=2)


def _makefile(root: str) -> str:
    return f"""\
.PHONY: assistant-status

# ── Assistant ────────────────────────────────────────────────────────────────

assistant-status:
\t@echo "=== Assistant Status ==="
\t@echo "Root: {root}"
\t@echo "Profile: $$(test -f ~/.copilot/assistant_profile.json && echo 'OK' || echo 'MISSING')"
\t@echo "Scheduler: $$(test -f {root}/assistant/memory/scheduler.json && echo 'OK' || echo 'MISSING')"
\t@echo "Readiness: ONLINE"
"""


# ── File manifest ────────────────────────────────────────────────────────────

def get_files(root: str) -> list[tuple[str, str]]:
    """Return list of (relative_path, content) tuples."""
    return [
        ("CLAUDE.md", _claude_md(root)),
        ("assistant/AGENTS.md", _agents_md(root)),
        ("assistant/CLAUDE.md", _assistant_claude_md(root)),
        ("assistant/ship_mind_persona.prompt", _persona_prompt()),
        ("assistant/config.json", _config_json(root)),
        ("assistant/memory/preferences.md", _preferences_md()),
        ("assistant/memory/README.md", _memory_readme()),
        ("assistant/memory/scheduler.json", _scheduler_json()),
        ("Makefile", _makefile(root)),
    ]


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bootstrap a personal assistant workspace.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Workspace root directory (default: current directory)",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip confirmation prompts",
    )
    args = parser.parse_args()

    root = args.root.expanduser().resolve()

    print(f"Bootstrap assistant workspace at: {root}\n")

    # Show what will be created
    files = get_files(str(root))
    dirs_to_create = [d for d in DIRS if not (root / d).exists()]
    files_to_create = [(p, c) for p, c in files if not (root / p).exists()]
    files_to_skip = [(p, c) for p, c in files if (root / p).exists()]

    if dirs_to_create:
        print("Directories to create:")
        for d in dirs_to_create:
            print(f"  + {d}/")
        print()

    if files_to_create:
        print("Files to create:")
        for p, _ in files_to_create:
            print(f"  + {p}")
        print()

    if files_to_skip:
        print("Files that already exist (skipping):")
        for p, _ in files_to_skip:
            print(f"  ~ {p}")
        print()

    if not dirs_to_create and not files_to_create:
        print("Nothing to do — workspace is already set up.")
        return

    if not args.yes:
        confirm = input("Proceed? [Y/n] ").strip().lower()
        if confirm and confirm != "y":
            print("Aborted.")
            sys.exit(1)

    # Create directories
    for d in dirs_to_create:
        (root / d).mkdir(parents=True, exist_ok=True)

    # Write files (skip existing)
    for path, content in files_to_create:
        full_path = root / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)

    print(f"\n✓ Workspace bootstrapped at {root}")
    print()
    print("Next steps:")
    print(f"  1. cd {root}")
    print("  2. claude                        # launch Claude Code")
    print("  3. assistant-sync inbox           # check for packets from work")
    print()
    print("To sync identity (if not already done):")
    print("  assistant-sync init --label home")
    print("  assistant-sync pair --code <CODE> --label home")


if __name__ == "__main__":
    main()
