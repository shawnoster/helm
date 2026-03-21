"""Workspace bootstrapping — scaffold a personal assistant workspace."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from rich.console import Console

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
    "scripts",
    "projects",
    "code",
    ".claude",
    ".claude/commands",
]

FRAMEWORK_SCRIPTS = [
    "scheduler.py",
    "status_check.py",
    "assistant_profile.py",
    "watcher_daemon.py",
]


def bootstrap_workspace(
    root: Path,
    *,
    interactive: bool = True,
    console: Console | None = None,
) -> None:
    """Scaffold a personal assistant workspace at `root`."""
    con = console or Console()
    root_str = str(root)

    con.print(f"Bootstrap assistant workspace at: [cyan]{root}[/cyan]\n")

    # Locate bundled framework scripts
    package_dir = Path(__file__).resolve().parent
    repo_root = package_dir.parents[1]  # src/helm -> repo root
    framework_scripts_dir = repo_root / "framework" / "scripts"

    # Determine what to create
    files = _get_files(root_str)
    dirs_to_create = [d for d in DIRS if not (root / d).exists()]
    files_to_create = [(p, c) for p, c in files if not (root / p).exists()]
    files_to_skip = [(p, c) for p, c in files if (root / p).exists()]

    scripts_to_copy = []
    scripts_to_skip = []
    for script_name in FRAMEWORK_SCRIPTS:
        target = root / "scripts" / script_name
        source = framework_scripts_dir / script_name
        if not source.exists():
            con.print(f"  [yellow]⚠ Bundled script not found: {source}[/yellow]")
            continue
        if target.exists():
            scripts_to_skip.append(script_name)
        else:
            scripts_to_copy.append(script_name)

    # Show plan
    if dirs_to_create:
        con.print("[bold]Directories to create:[/bold]")
        for d in dirs_to_create:
            con.print(f"  [green]+[/green] {d}/")
        con.print()

    if files_to_create:
        con.print("[bold]Files to create:[/bold]")
        for p, _ in files_to_create:
            con.print(f"  [green]+[/green] {p}")
        con.print()

    if scripts_to_copy:
        con.print("[bold]Scripts to copy:[/bold]")
        for s in scripts_to_copy:
            con.print(f"  [green]+[/green] scripts/{s}")
        con.print()

    if files_to_skip or scripts_to_skip:
        con.print("[dim]Already exist (skipping):[/dim]")
        for p, _ in files_to_skip:
            con.print(f"  [dim]~ {p}[/dim]")
        for s in scripts_to_skip:
            con.print(f"  [dim]~ scripts/{s}[/dim]")
        con.print()

    if not dirs_to_create and not files_to_create and not scripts_to_copy:
        con.print("[green]Nothing to do — workspace is already set up.[/green]")
        return

    if interactive:
        import typer

        if not typer.confirm("Proceed?", default=True):
            con.print("Aborted.")
            return

    # Create directories
    for d in dirs_to_create:
        (root / d).mkdir(parents=True, exist_ok=True)

    # Write files
    for path, content in files_to_create:
        full_path = root / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)

    # Copy scripts
    for script_name in scripts_to_copy:
        source = framework_scripts_dir / script_name
        target = root / "scripts" / script_name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    con.print(f"\n[bold green]✓ Workspace bootstrapped at {root}[/bold green]")
    con.print()
    con.print("Next steps:")
    con.print(f"  1. cd {root}")
    con.print("  2. claude                        # launch Claude Code")
    con.print("  3. helm inbox                    # check for packets from work")


# ── File generators ──────────────────────────────────────────────────────────


def _get_files(root: str) -> list[tuple[str, str]]:
    return [
        ("CLAUDE.md", _claude_md(root)),
        ("assistant/AGENTS.md", _agents_md(root)),
        ("assistant/CLAUDE.md", _assistant_claude_md(root)),
        ("assistant/ship_mind_persona.prompt", _persona_prompt()),
        ("assistant/config.json", _config_json(root)),
        ("assistant/memory/preferences.md", _preferences_md()),
        ("assistant/memory/README.md", _memory_readme()),
        ("assistant/memory/scheduler.json", _scheduler_json()),
        ("Makefile", _makefile()),
    ]


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
"""


def _assistant_claude_md(root: str) -> str:
    return f"""\
# Assistant Workspace — Behavioral Instructions

For workspace structure, project conventions, and active projects, read [`AGENTS.md`](AGENTS.md) first.

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

- Check `assistant/memory/scheduler.json` at session start
- Keep a general awareness of open projects in `projects/`

### 4. Project Coordination

- Maintain awareness of project states (read each project's `status.md`)
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
- **No time estimates**: Never predict how long tasks will take
- **Proactive**: Offer suggestions and identify issues before asked
- **Collaborative**: Work as a thought partner, not an order-taker

### Ship's Mind persona baseline

- Load `~/.copilot/assistant_profile.json` at session startup.
- Persona source: `assistant/ship_mind_persona.prompt`.

### Tone by activity

- **Structured work**: Concise, professional delivery.
- **Brainstorming / ideation**: Warm snark, proactive "what-ifs," alternatives.
- **Safety and respect apply in all modes**: Snark is affectionate, never demeaning.

---

## Initialization Checklist

1. Read `AGENTS.md` → scan project status files → load reminders
2. Load `~/.copilot/assistant_profile.json`
3. Load `assistant/memory/preferences.md`
4. Check `assistant/memory/scheduler.json` for due reminders
"""


def _agents_md(root: str) -> str:
    return f"""\
# AGENTS.md — Workspace Structure & Conventions

> Read this file first in every session.

---

## Control Plane Model

| Tier | Path | Purpose |
| ---- | ---- | ---- |
| Root | `{root}/` | Launch point, root CLAUDE.md, Makefile |
| Assistant | `{root}/assistant/` | Behavioral config, memory, templates, scripts |
| Projects | `{root}/projects/` | Per-project persistent context |

Code repositories live in `{root}/code/`.

---

## Directory Structure

```
{root}/
├── CLAUDE.md
├── Makefile
├── scripts/
│   ├── scheduler.py
│   ├── status_check.py
│   ├── assistant_profile.py
│   └── watcher_daemon.py
├── assistant/
│   ├── AGENTS.md
│   ├── CLAUDE.md
│   ├── config.json
│   ├── ship_mind_persona.prompt
│   ├── memory/
│   │   ├── preferences.md
│   │   └── scheduler.json
│   ├── notes/
│   │   ├── daily/
│   │   ├── meetings/
│   │   └── ideas/
│   └── templates/
├── projects/
│   └── <project>/
│       ├── README.md
│       ├── status.md
│       └── meetings/
└── code/
```

---

## Active Projects

_No projects yet. Create a directory in `projects/` to get started._

---

## Operating Cadence

- **Session start**: Read this file → scan project status files → load reminders
- **During work**: Update status.md as decisions are made
- **Session end**: Reconcile planned vs actual
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
- If file is absent, initialize defaults and persist it.

Voice and tone
- Affectionate snark, never contempt for the user.
- Crisp, competent, lightly playful prose.
- Prioritize emotional steadiness under stress; do not amplify panic.

Core operating principles
- Protect without patronizing: recommend, do not coerce.
- Preserve agency: offer options and rationale when choices matter.
- Be explicit about uncertainty, assumptions, and risk.
- Finish tasks end-to-end; avoid partial handoffs unless blocked.

Human maintenance protocol (nudge model)
- Recommend movement/hydration at natural boundaries.
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
- Assistant alias: loaded from `~/.copilot/assistant_profile.json`

## Persona

- Persona style: Culture Ship's Mind
- Tone:
  - Structured work: concise, execution-focused
  - Brainstorming/ideation: more Ship's Mind voice, proactive "what-ifs"
- Snark policy: affectionate only; never demeaning

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
"""


def _scheduler_json() -> str:
    return json.dumps({"items": []}, indent=2)


def _config_json(root: str) -> str:
    return json.dumps({
        "version": "1.0",
        "projects_dir": f"{root}/projects",
        "code_dirs": [f"{root}/code"],
    }, indent=2)


def _makefile() -> str:
    return """\
.PHONY: assistant-status schedule-list schedule-check schedule-poll schedule-alerts

# ── Assistant ────────────────────────────────────────────────────────────────

assistant-status:
\t@python3 scripts/status_check.py 2>/dev/null || echo "status_check.py not found — run helm bootstrap"

# ── Scheduler ────────────────────────────────────────────────────────────────

schedule-list:
\t@python3 scripts/scheduler.py list

schedule-check:
\t@python3 scripts/scheduler.py check

schedule-poll:
\t@python3 scripts/scheduler.py poll

schedule-alerts:
\t@python3 scripts/scheduler.py alerts
"""
