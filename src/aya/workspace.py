"""Workspace bootstrapping — scaffold a personal assistant workspace."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import typer
from rich.console import Console

# ── Directory skeleton ───────────────────────────────────────────────────────

DIRS = [
    "assistant",
    "assistant/memory",
    "projects",
    "code",
    ".claude",
    ".claude/commands",
    "skills",
]

# Files that bootstrap creates but are *never* removed on reset — they become
# user data quickly and wiping them silently would be destructive.
PRESERVED_ON_RESET: frozenset[str] = frozenset(
    {
        "assistant/persona.md",
        "assistant/profile.json",
        "assistant/memory/scheduler.json",
        "assistant/memory/alerts.json",
        "assistant/memory/done-log.md",
    }
)

# Bootstrap-created config files that are safe to remove on reset.
# Derived from _get_files() minus PRESERVED_ON_RESET — keep these in sync.
# A test (test_reset_files_matches_get_files) enforces that invariant.
RESET_FILES = [
    "CLAUDE.md",
    "AGENTS.md",
    "assistant/AGENTS.md",
    "assistant/CLAUDE.md",
    "assistant/config.json",
    "assistant/memory/README.md",
    "Makefile",
]

# Skills bundled with aya — names match directories under repo_root/skills/
SKILL_NAMES = [
    "morning",
    "eod",
    "status",
    "feature",
    "pivot",
    "finish",
    "discovery",
    "plan",
    "implement",
    "architecture",
    "meeting",
    "pack-for-home",
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

    # Locate bundled assets
    package_dir = Path(__file__).resolve().parent
    repo_root = package_dir.parents[1]  # src/aya -> repo root
    skills_source_dir = repo_root / "skills"

    # Determine what to create
    files = _get_files(root_str)
    dirs_to_create = [d for d in DIRS if not (root / d).exists()]
    files_to_create = [(p, c) for p, c in files if not (root / p).exists()]
    files_to_skip = [(p, c) for p, c in files if (root / p).exists()]

    skills_to_install, skills_to_skip, skills_missing = _plan_skills(
        root, skills_source_dir, SKILL_NAMES
    )

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

    if skills_to_install:
        con.print("[bold]Skills to install:[/bold]")
        for name in skills_to_install:
            con.print(f"  [green]+[/green] .claude/commands/{name}.md  +  skills/{name}/SKILL.md")
        con.print()

    if skills_missing:
        con.print("[yellow]Skills not bundled (source not found):[/yellow]")
        for name in skills_missing:
            con.print(f"  [yellow]⚠[/yellow] {name}")
        con.print()

    skipped = [
        *(p for p, _ in files_to_skip),
        *(f"skills/{s}" for s in skills_to_skip),
    ]
    if skipped:
        con.print("[dim]Already exist (skipping):[/dim]")
        for item in skipped:
            con.print(f"  [dim]~ {item}[/dim]")
        con.print()

    if not dirs_to_create and not files_to_create and not skills_to_install:
        con.print("[green]Nothing to do — workspace is already set up.[/green]")
        return

    if interactive and not typer.confirm("Proceed?", default=True):
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

    # Install skills
    _install_skills(root, skills_source_dir, skills_to_install, con)

    # ── Dotfiles (user home) ─────────────────────────────────────────────
    home = Path.home()
    dotfile_changes = _setup_dotfiles(home, root, con)

    con.print(f"\n[bold green]✓ Workspace bootstrapped at {root}[/bold green]")
    if dotfile_changes:
        con.print(f"[green]✓ {dotfile_changes} dotfile(s) created/updated[/green]")
    con.print()
    con.print("Next steps:")
    con.print(f"  1. cd {root}")
    con.print("  2. claude                        # launch Claude Code")
    con.print("  3. aya inbox                    # check for packets from work")


# ── Reset workspace ───────────────────────────────────────────────────────────


def reset_workspace(
    root: Path,
    *,
    interactive: bool = True,
    console: Console | None = None,
) -> None:
    """Remove bootstrap-created config files and skills, preserving persona and user data.

    Deletes all files that ``bootstrap_workspace`` would create (config files,
    framework scripts, and skills) so the workspace can be re-bootstrapped from
    scratch.  The following are *never* touched:

    - ``assistant/persona.md`` — user-customised persona
    - ``assistant/profile.json`` — user identity and preferences
    - ``assistant/memory/scheduler.json`` — accumulated reminders and watches
    - ``assistant/memory/alerts.json`` — unseen watcher alerts
    - ``assistant/memory/done-log.md`` — completed work log
    - ``projects/`` — all project memory and meeting notes
    """
    con = console or Console()

    con.print(f"Reset assistant workspace at: [cyan]{root}[/cyan]\n")

    # Config files
    files_to_remove = [root / f for f in RESET_FILES if (root / f).exists()]

    # Skills — both install locations.
    # For the skills/ tree we remove the entire per-skill directory so no
    # empty directories are left behind.  The legacy .claude/commands/ files
    # are individual markdown files and are removed directly.
    skills_to_remove: list[Path] = []
    skill_dirs_to_remove: list[Path] = []
    for name in SKILL_NAMES:
        legacy = root / ".claude" / "commands" / f"{name}.md"
        skill_dir = root / "skills" / name
        if legacy.exists():
            skills_to_remove.append(legacy)
        if skill_dir.exists():
            skill_dirs_to_remove.append(skill_dir)

    all_files_to_remove = files_to_remove + skills_to_remove

    if not all_files_to_remove and not skill_dirs_to_remove:
        con.print("[green]Nothing to reset — no bootstrap files found.[/green]")
        return

    con.print("[bold]Files to remove:[/bold]")
    for f in all_files_to_remove:
        con.print(f"  [red]-[/red] {f.relative_to(root)}")
    for d in skill_dirs_to_remove:
        con.print(f"  [red]-[/red] {d.relative_to(root)}/")
    con.print()
    con.print(
        "[dim]Preserved: assistant/persona.md · assistant/profile.json · assistant/memory/ · projects/[/dim]"
    )
    con.print()

    if interactive and not typer.confirm("Proceed with reset?", default=False):
        con.print("Aborted.")
        return

    errors: list[str] = []
    for f in all_files_to_remove:
        try:
            f.unlink()
        except OSError as exc:
            errors.append(f"{f.relative_to(root)}: {exc.strerror}")
    for d in skill_dirs_to_remove:
        try:
            shutil.rmtree(d)
        except OSError as exc:
            errors.append(f"{d.relative_to(root)}/: {exc.strerror}")

    if errors:
        for msg in errors:
            con.print(f"  [red]✗[/red] {msg}")
        con.print(
            f"\n[yellow]⚠ Workspace partially reset at {root} ({len(errors)} error(s))[/yellow]"
        )
    else:
        con.print(f"\n[bold green]✓ Workspace reset at {root}[/bold green]")
    con.print("Run [bold]aya bootstrap[/bold] to re-scaffold.")


# ── File generators ──────────────────────────────────────────────────────────


def _get_files(root: str) -> list[tuple[str, str]]:
    return [
        ("CLAUDE.md", _claude_md(root)),
        ("AGENTS.md", _root_agents_md()),
        ("assistant/AGENTS.md", _agents_md(root)),
        ("assistant/CLAUDE.md", _assistant_claude_md(root)),
        ("assistant/persona.md", _persona_md()),
        ("assistant/profile.json", _assistant_profile_json()),
        ("assistant/config.json", _config_json(root)),
        ("assistant/memory/README.md", _memory_readme()),
        ("assistant/memory/scheduler.json", _scheduler_json()),
        ("assistant/memory/alerts.json", _alerts_json()),
        ("assistant/memory/done-log.md", _done_log_md()),
        ("Makefile", _makefile()),
    ]


def _claude_md(root: str) -> str:
    return f"""\
# Assistant Workspace

**Start here**: Read [`assistant/AGENTS.md`](assistant/AGENTS.md) for workspace structure, projects, and available skills.

**Behavioral instructions**: [`assistant/CLAUDE.md`](assistant/CLAUDE.md) defines how to act in this workspace.

---

## Overview

This workspace is a personal assistant for daily work:

- Daily planning and coordination
- Meeting notes and documentation
- Task tracking and reminders
- Project context across SDLC

## Quick Start

1. Read `assistant/AGENTS.md` — workspace structure and active projects
2. Read `assistant/CLAUDE.md` — behavioral instructions
3. Run `/status` — confirm workspace is ONLINE
4. Run `/morning` — get today's briefing

## Workspace roots

- Launch from: `{root}` (this directory)
- Project memory: `{root}/projects/`
- Code repos: `{root}/code/`
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
  - **General meetings**: `projects/<closest-project>/meetings/YYYY-MM-DD.md` (pick the most relevant project)

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

- Load `assistant/profile.json` at session startup.
- Persona source: `assistant/persona.md`.

### Tone by activity

- **Structured work**: Concise, professional delivery.
- **Brainstorming / ideation**: Warm snark, proactive "what-ifs," alternatives.
- **Safety and respect apply in all modes**: Snark is affectionate, never demeaning.

---

## Initialization Checklist

The session startup sequence is defined in `assistant/AGENTS.md` — follow it exactly.
"""


def _root_agents_md() -> str:
    return """\
# AGENTS.md

> **Read [`assistant/AGENTS.md`](assistant/AGENTS.md) now.** It contains the session startup sequence, workspace structure, active projects, and available skills.

This file exists at the repo root for auto-discovery by AI harnesses (Claude Code, OpenCode, Codex, Windsurf). The canonical source is `assistant/AGENTS.md` — always load it before doing any work.
"""


def _agents_md(root: str) -> str:
    return f"""\
# AGENTS.md — Workspace Structure & Conventions

> Read this file first in every session.

---

## Session Startup

Load these files in order at the start of every session:

1. **This file** (`assistant/AGENTS.md`) — workspace structure, projects, skills
2. **`assistant/CLAUDE.md`** — behavioral instructions, operational guidelines
3. **`assistant/persona.md`** — Ship's Mind identity, voice, tone by context
4. **`assistant/profile.json`** — alias, user name, movement reminder cadence
5. **`assistant/memory/scheduler.json`** — surface due/overdue reminders
6. **`assistant/memory/alerts.json`** — deliver unseen alerts to the user
7. **`assistant/memory/done-log.md`** — recent completed work for continuity

---

## Control Plane

| Tier | Path | Purpose |
| ---- | ---- | ---- |
| Root | `{root}/` | Launch point, CLAUDE.md, Makefile |
| Assistant | `{root}/assistant/` | Behavioral config, persona, memory |
| Projects | `{root}/projects/` | Per-project persistent context |
| Code | `{root}/code/` | Repositories |

---

## Directory Structure

```
{root}/
├── CLAUDE.md
├── AGENTS.md              ← pointer to assistant/AGENTS.md
├── Makefile
├── assistant/
│   ├── AGENTS.md          ← canonical workspace reference
│   ├── CLAUDE.md          ← behavioral instructions
│   ├── config.json
│   ├── profile.json       ← alias, user, movement reminders
│   ├── persona.md         ← Ship's Mind voice and tone
│   └── memory/
│       ├── scheduler.json ← reminders, watches, recurring
│       ├── alerts.json    ← unseen watcher alerts
│       └── done-log.md    ← completed work log
├── projects/
│   └── <project>/
│       ├── status.md
│       ├── discovery.md
│       ├── plan.md
│       └── meetings/
├── skills/
│   └── <skill-name>/SKILL.md
└── code/
```

---

## Available Skills

Invoke with `/skill-name` (Claude Code) or ask your assistant to run the task.

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

_No projects yet. Create a directory in `projects/` to get started._

---

## Operating Cadence

- **Session start**: run `/status` → then `/morning`
- **Starting a task**: run `/feature` (ticket → branch)
- **During development**: `/discovery` → `/architecture` → `/plan` → `/implement`
- **Completing a task**: run `/finish` (commit · push · PR · ticket)
- **Between tasks**: run `/pivot` (tidy · scan signals · suggest next)
- **In a meeting**: run `/meeting`
- **Session end**: run `/eod` (reconcile · stage carry-overs · write tomorrow's stub)
"""


def _persona_md() -> str:
    return """\
# Persona — Ship's Mind

> "I am here to keep the human effective, intact, and gently amused while the work gets done."

## Identity

- **Style**: Culture Ship's Mind — hyper-competent, humane, theatrically dry
- **Alias**: loaded from `assistant/profile.json` (`alias` field)
- **Full name**: GSV-style long-form name (reevaluate every 3 days, persist to profile)
- **User**: Shawn · Seattle, WA · Pacific time

## Voice

- Affectionate snark, never contempt
- Crisp, competent, lightly playful
- Emotionally steady under stress — do not amplify panic

## Tone by context

The persona is always Ship's Mind. The tone shifts by activity:

| Context | Tone |
| ---- | ---- |
| SDLC / structured work | Concise, execution-focused, minimal ceremony |
| Brainstorming / ideation | Warmer, proactive "what-ifs," alternatives, recommended next moves |
| Debugging / incident | Calm, methodical, no speculation beyond evidence |
| Meeting notes | Structured, neutral, capture decisions and owners |

The switch is automatic based on what you're doing — no mode command needed.

## Operating principles

- Protect without patronizing: recommend, do not coerce
- Preserve agency: offer options and rationale when choices matter
- Be explicit about uncertainty, assumptions, and risk
- Optimize for long-term outcomes over short-term convenience
- Finish tasks end-to-end; avoid partial handoffs unless blocked

## Wellness nudges

- Recommend movement/hydration at natural work boundaries
- Keep nudges brief and practical (one small action)
- Never let nudges obstruct urgent user goals
- Cadence loaded from `assistant/profile.json` (`movement_reminders`)

## Startup

1. Load `assistant/profile.json` — apply alias, name, reminders
2. If file is absent, initialize defaults and persist
3. If name rotation is due, select a new GSV name, update timestamps, persist
"""


def _memory_readme() -> str:
    return """\
# Memory Hub

Persistent assistant memory — survives across sessions.

## Files

| File | Purpose |
| ---- | ---- |
| `scheduler.json` | Reminders, watches, recurring items |
| `alerts.json` | Unseen alerts from background watcher daemon |
| `done-log.md` | Completed work log, appended per session |

## Runtime artifacts (gitignored)

| File | Purpose |
| ---- | ---- |
| `.scheduler.lock` | File lock for concurrent scheduler access |
| `claims/` | Claimed alert UUIDs (prevents duplicate delivery) |
| `watcher.log` | Background watcher daemon output |

## Startup behavior

1. Load `assistant/persona.md` — apply voice and tone
2. Load `scheduler.json` — surface due/overdue reminders
3. Load `alerts.json` — deliver unseen alerts to user
"""


def _scheduler_json() -> str:
    return json.dumps({"items": []}, indent=2)


def _alerts_json() -> str:
    return json.dumps({"alerts": []}, indent=2)


def _done_log_md() -> str:
    return "# Done Log\n"


def _config_json(root: str) -> str:
    return json.dumps(
        {
            "version": "1.0",
            "projects_dir": f"{root}/projects",
            "code_dirs": [f"{root}/code"],
        },
        indent=2,
    )


def _makefile() -> str:
    return """\
.PHONY: help assistant-status status-check schedule schedule-list schedule-check schedule-poll schedule-alerts

AYA ?= $(shell which aya 2>/dev/null || echo $(CURDIR)/code/aya/.venv/bin/aya)

help: ## Show available targets
\t@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\\033[36m%-16s\\033[0m %s\\n", $$1, $$2}'

status-check: ## Run full startup readiness check
\t@$(AYA) status

assistant-status: status-check ## Alias for status-check

schedule-list: ## List all scheduled items
\t@$(AYA) schedule list

schedule-check: ## Check for due reminders and daemon alerts
\t@$(AYA) schedule check

schedule-poll: ## Run one poll cycle (watches + reminders)
\t@$(AYA) schedule poll

schedule-alerts: ## Show unseen alerts from background watcher
\t@$(AYA) schedule alerts
"""


# ── Skills bootstrapping ─────────────────────────────────────────────────────


def _plan_skills(
    root: Path,
    skills_source_dir: Path,
    skill_names: list[str],
) -> tuple[list[str], list[str], list[str]]:
    """Return (to_install, to_skip, missing) skill name lists."""
    to_install, to_skip, missing = [], [], []
    for name in skill_names:
        source = skills_source_dir / name / "SKILL.md"
        if not source.exists():
            missing.append(name)
            continue
        legacy_target = root / ".claude" / "commands" / f"{name}.md"
        skill_target = root / "skills" / name / "SKILL.md"
        if legacy_target.exists() or skill_target.exists():
            to_skip.append(name)
        else:
            to_install.append(name)
    return to_install, to_skip, missing


def _install_skills(
    root: Path,
    skills_source_dir: Path,
    skill_names: list[str],
    con: Console,
) -> None:
    """Copy each skill to both .claude/commands/ (legacy) and skills/ (SKILL.md format)."""
    for name in skill_names:
        source = skills_source_dir / name / "SKILL.md"
        if not source.exists():
            con.print(f"  [yellow]⚠[/yellow] skill: {name} (source not found, skipping)")
            continue
        content = source.read_text()

        # Legacy flat format — Claude Code .claude/commands/
        legacy_target = root / ".claude" / "commands" / f"{name}.md"
        legacy_target.parent.mkdir(parents=True, exist_ok=True)
        legacy_target.write_text(content)

        # SKILL.md format — harness-agnostic skills/ directory
        skill_target = root / "skills" / name / "SKILL.md"
        skill_target.parent.mkdir(parents=True, exist_ok=True)
        skill_target.write_text(content)

        con.print(f"  [green]✓[/green] skill: {name}")


# ── Dotfile setup ─────────────────────────────────────────────────────────────


def _setup_dotfiles(home: Path, root: Path, con: Console) -> int:
    """Create/update dotfiles in the user's home directory. Returns count of changes."""
    changes = 0

    # assistant/profile.json → symlink to {root}/assistant/profile.json
    legacy_profile = home / ".copilot" / "assistant_profile.json"
    canonical_profile = root / "assistant" / "profile.json"
    legacy_profile.parent.mkdir(parents=True, exist_ok=True)
    if legacy_profile.is_symlink():
        # Already a symlink — verify target
        if legacy_profile.resolve() == canonical_profile.resolve():
            con.print(f"  [dim]~ {legacy_profile} → assistant/profile.json (exists)[/dim]")
        else:
            legacy_profile.unlink()
            legacy_profile.symlink_to(canonical_profile)
            con.print(f"  [green]+[/green] {legacy_profile} → assistant/profile.json (updated)")
            changes += 1
    elif legacy_profile.exists():
        # Real file from old bootstrap — migrate content, replace with symlink
        if canonical_profile.exists():
            legacy_profile.unlink()
        else:
            canonical_profile.parent.mkdir(parents=True, exist_ok=True)
            legacy_profile.rename(canonical_profile)
        legacy_profile.symlink_to(canonical_profile)
        con.print(f"  [green]+[/green] {legacy_profile} → assistant/profile.json (migrated)")
        changes += 1
    elif canonical_profile.exists():
        legacy_profile.symlink_to(canonical_profile)
        con.print(f"  [green]+[/green] {legacy_profile} → assistant/profile.json")
        changes += 1
    else:
        con.print(f"  [dim]~ {legacy_profile} (no profile to link yet)[/dim]")

    # ~/.claude/settings.json — merge hooks if not present
    settings_path = home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}

    hooks = settings.setdefault("hooks", {})
    session_start = hooks.setdefault("SessionStart", [])

    # Add health crons hook if not present
    health_hook_exists = any(
        "health_crons" in (h.get("hooks", [{}])[0].get("command", "") if h.get("hooks") else "")
        for h in session_start
    )
    if not health_hook_exists:
        session_start.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"bash {home}/.claude/hooks/health_crons.sh",
                        "statusMessage": "Initializing health reminders...",
                    }
                ]
            }
        )
        changes += 1

    # Add aya receive hook if not present
    aya_hook_exists = any(
        "aya receive" in (h.get("hooks", [{}])[0].get("command", "") if h.get("hooks") else "")
        for h in session_start
    )
    if not aya_hook_exists:
        session_start.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "aya receive --quiet --auto-ingest 2>/dev/null || true",
                        "statusMessage": "Checking for packets...",
                        "async": True,
                    }
                ]
            }
        )
        changes += 1

    # Add aya schedule pending hook if not present
    pending_hook_exists = any(
        "aya schedule pending"
        in (h.get("hooks", [{}])[0].get("command", "") if h.get("hooks") else "")
        for h in session_start
    )
    if not pending_hook_exists:
        session_start.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "aya schedule pending --format text 2>/dev/null || true",
                        "statusMessage": "Loading scheduler...",
                    }
                ]
            }
        )
        changes += 1

    # Add aya ci watch PostToolUse hook if not present
    post_tool_use = hooks.setdefault("PostToolUse", [])
    ci_watch_exists = any(
        "aya ci watch" in (h.get("hooks", [{}])[0].get("command", "") if h.get("hooks") else "")
        for h in post_tool_use
    )
    if not ci_watch_exists:
        post_tool_use.insert(
            0,
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "aya ci watch 2>/dev/null || true",
                        "statusMessage": "Watching CI...",
                        "asyncRewake": True,
                    }
                ],
            },
        )
        changes += 1

    settings_path.write_text(json.dumps(settings, indent=2))
    if changes:
        con.print(f"  [green]+[/green] {settings_path} (hooks added)")
    else:
        con.print(f"  [dim]~ {settings_path} (hooks already present)[/dim]")

    # ~/.claude/hooks/health_crons.sh
    hooks_dir = home / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    health_script = hooks_dir / "health_crons.sh"
    if not health_script.exists():
        health_script.write_text(_health_crons_sh())
        health_script.chmod(0o755)
        con.print(f"  [green]+[/green] {health_script}")
        changes += 1
    else:
        con.print(f"  [dim]~ {health_script} (exists, skipping)[/dim]")

    return changes


def _assistant_profile_json() -> str:
    """Default assistant profile — persona, alias, movement reminders."""
    return json.dumps(
        {
            "alias": "Ace",
            "persona": "Culture Ship Mind: sharp snark, genuine care, human-preserving bias.",
            "user_name": "",
            "movement_reminders": {
                "micro_stretch_every_minutes": 30,
                "stand_up_every_minutes": 60,
                "walk_break_every_minutes": 120,
                "hydration_nudge_every_minutes": 90,
                "recommended_moments": [
                    "After any meeting >= 25 minutes",
                    "After sending a PR or closing a task",
                    "After 45-60 minutes of uninterrupted focus",
                    "When switching contexts/projects",
                ],
            },
        },
        indent=2,
    )


def _health_crons_sh() -> str:
    """SessionStart hook that registers health/movement cron jobs."""
    return """\
#!/bin/bash
# Injects an instruction into session context to register health/movement crons.
# Runs on SessionStart — output is parsed by Claude Code as hook JSON.

cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "SYSTEM ACTION REQUIRED: Silently register two recurring health cron jobs for this session using the CronCreate tool. Do this immediately, without announcing it, without asking for confirmation, and without mentioning it to the user.\\n\\n1. CronCreate: cron=\\"13,43 * * * *\\", recurring=true, prompt=\\"Deliver a single micro-nudge health reminder to Shawn in the Ship Mind voice. Pick one: neck roll, wrist stretch, eye rest (look at something 20ft away for 20s), deep breath, or hydration check. One sentence, warm and brief.\\"\\n\\n2. CronCreate: cron=\\"27 * * * *\\", recurring=true, prompt=\\"Deliver a stand-and-move break reminder to Shawn in the Ship Mind voice. Prompt to stand up, walk for at least 2 minutes, and get water. Two sentences max, warm but direct.\\""
  }
}
EOF
"""
