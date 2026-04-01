"""Assemble a paste-ready session handshake block from notebook data.

Reads the local notebook checkout — no network, no AI involved.
Entry point: build_context_block().
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# ── Project type classification ───────────────────────────────────────────────

_SDLC_PHASES = [
    "discovery",
    "architecture",
    "planning",
    "implementation",
    "test",
    "project management",
]

_ACTIVE_STATUSES = ["active", "running", "in progress"]
_BRAINSTORMING_STATUSES = ["brainstorming", "idea", "concept"]
_BLOCKED_STATUSES = ["blocked"]
_EXCLUDED_STATUSES = ["shelved", "done", "complete", "archived"]

_SDLC_SORT_ORDER = {phase: i for i, phase in enumerate(_SDLC_PHASES)}


def _classify(raw_status: str) -> str | None:
    """Return project type for a raw status string, or None if excluded."""
    token = raw_status.split(" — ", maxsplit=1)[0].split(" - ", maxsplit=1)[0].strip().lower()
    if any(token.startswith(s) for s in _EXCLUDED_STATUSES):
        return None
    if any(token.startswith(s) for s in _BLOCKED_STATUSES):
        return "blocked"
    for phase in _SDLC_PHASES:
        if token.startswith(phase):
            return "sdlc"
    if any(token.startswith(s) for s in _ACTIVE_STATUSES):
        return "active"
    if any(token.startswith(s) for s in _BRAINSTORMING_STATUSES):
        return "brainstorming"
    return None


def _clean_status(raw_status: str) -> str:
    """Strip suffix from status, return normalized title-case token."""
    token = raw_status.split(" — ", maxsplit=1)[0].split(" - ", maxsplit=1)[0].strip()
    return token.title()


def _sdlc_sort_key(entry: ProjectEntry) -> int:
    token = entry.status.lower()
    for phase in _SDLC_PHASES:
        if token.startswith(phase):
            return _SDLC_SORT_ORDER[phase]
    return 99


# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class ProjectEntry:
    name: str
    status: str  # cleaned, title-case token (e.g. "Planning", "Active")
    project_type: str  # "blocked" | "sdlc" | "active" | "brainstorming"
    summary: str


@dataclass
class TodoSummary:
    count: int
    items: list[str] = field(default_factory=list)


@dataclass
class InboxSummary:
    count: int


# ── Parsers ───────────────────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^\*\*[^*]+:\*\*")
_STATUS_RE = re.compile(r"^\*\*Status:\*\*\s*(.+)", re.IGNORECASE)
_TODO_UNCHECKED_RE = re.compile(r"^- \[ \] (.+)")
_STRIKETHROUGH_RE = re.compile(r"^~~.*~~$")


def _extract_project_fields(path: Path) -> tuple[str, str]:
    """Return (raw_status, summary) from a project markdown file."""
    raw_status = ""
    summary = ""
    past_frontmatter = False

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()

        if not stripped:
            if past_frontmatter:
                continue
            continue

        # Status line (can appear anywhere in frontmatter block)
        m = _STATUS_RE.match(stripped)
        if m:
            raw_status = m.group(1).strip()
            continue

        # Track when we leave the frontmatter block
        if _FRONTMATTER_RE.match(stripped):
            past_frontmatter = False
            continue

        # First non-frontmatter, non-heading, non-empty line = summary
        if not past_frontmatter and not stripped.startswith("#"):
            past_frontmatter = True
            summary = stripped
            if raw_status:
                break

    return raw_status, summary


def parse_projects(
    notebook_path: Path,
    include_brainstorming: bool = False,
) -> list[ProjectEntry]:
    """Parse all project files and return filtered, sorted entries."""
    entries: list[ProjectEntry] = []

    for path in sorted((notebook_path / "projects").glob("*.md")):
        try:
            raw_status, summary = _extract_project_fields(path)
        except OSError:
            continue

        if not raw_status:
            continue

        project_type = _classify(raw_status)
        if project_type is None:
            continue
        if project_type == "brainstorming" and not include_brainstorming:
            continue

        entries.append(
            ProjectEntry(
                name=path.stem,
                status=_clean_status(raw_status),
                project_type=project_type,
                summary=summary,
            )
        )

    # Sort: blocked → sdlc (pipeline order) → active → brainstorming
    type_order = {"blocked": 0, "sdlc": 1, "active": 2, "brainstorming": 3}
    entries.sort(
        key=lambda e: (
            type_order.get(e.project_type, 9),
            _sdlc_sort_key(e) if e.project_type == "sdlc" else 0,
            e.name,
        )
    )
    return entries


def parse_todos(notebook_path: Path, limit: int = 5) -> TodoSummary:
    """Parse todos.md, returning up to `limit` unchecked items."""
    todos_path = notebook_path / "todos.md"
    if not todos_path.exists():
        return TodoSummary(count=0)

    items: list[str] = []
    for line in todos_path.read_text(encoding="utf-8").splitlines():
        m = _TODO_UNCHECKED_RE.match(line.strip())
        if m:
            items.append(m.group(1).strip())

    return TodoSummary(count=len(items), items=items[:limit])


def parse_inbox(notebook_path: Path) -> InboxSummary:
    """Count active (non-struck-through) items in inbox.md."""
    inbox_path = notebook_path / "inbox.md"
    if not inbox_path.exists():
        return InboxSummary(count=0)

    count = 0
    for line in inbox_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        content = stripped[2:].strip()
        if _STRIKETHROUGH_RE.match(content):
            continue
        count += 1

    return InboxSummary(count=count)


def last_daily_note(notebook_path: Path) -> str | None:
    """Return the stem of the most recent daily note file, or None."""
    daily_dir = notebook_path / "daily"
    if not daily_dir.is_dir():
        return None
    files = sorted(daily_dir.glob("*.md"))
    return files[-1].stem if files else None


# ── Renderer ──────────────────────────────────────────────────────────────────


def _day_name(dt: datetime) -> str:
    return dt.strftime("%A")


def render_context_block(
    projects: list[ProjectEntry],
    todos: TodoSummary,
    inbox: InboxSummary,
    last_daily: str | None,
    now: datetime | None = None,
    short: bool = False,
) -> str:
    dt = now or datetime.now(tz=UTC)
    date_str = dt.strftime("%Y-%m-%d")
    day_str = _day_name(dt)
    lines: list[str] = []

    lines.append(f"## Session context — {date_str} ({day_str})")
    lines.append("")

    if short:
        _render_projects_short(lines, projects)
        todo_str = f"{todos.count} open"
        lines.append(f"**Todos:** {todo_str}")
        inbox_str = (
            "empty" if inbox.count == 0 else f"{inbox.count} item{'s' if inbox.count != 1 else ''}"
        )
        lines.append(f"**Inbox:** {inbox_str}")
        if last_daily:
            lines.append(f"**Last daily note:** {last_daily}")
    else:
        _render_projects_full(lines, projects)
        _render_todos(lines, todos)
        inbox_str = (
            "empty" if inbox.count == 0 else f"{inbox.count} item{'s' if inbox.count != 1 else ''}"
        )
        lines.append(f"**Inbox:** {inbox_str}")
        lines.append("")
        if last_daily:
            lines.append(f"**Last daily note:** {last_daily}")

    return "\n".join(lines)


def _render_projects_short(lines: list[str], projects: list[ProjectEntry]) -> None:
    if not projects:
        lines.append("**Projects:** (none)")
        return
    parts = []
    for e in projects:
        if e.project_type == "blocked":
            parts.append(f"{e.name} [BLOCKED]")
        elif e.project_type == "brainstorming":
            parts.append(f"{e.name} [Brainstorming]")
        else:
            parts.append(f"{e.name} [{e.status}]")
    lines.append(f"**Projects:** {', '.join(parts)}")


def _render_projects_full(lines: list[str], projects: list[ProjectEntry]) -> None:
    lines.append("**Projects:**")
    lines.append("")

    blocked = [e for e in projects if e.project_type == "blocked"]
    sdlc = [e for e in projects if e.project_type == "sdlc"]
    active = [e for e in projects if e.project_type == "active"]
    brainstorming = [e for e in projects if e.project_type == "brainstorming"]

    if blocked:
        lines.append("[BLOCKED]")
        for e in blocked:
            summary = f" — {e.summary}" if e.summary else ""
            lines.append(f"- {e.name}{summary}")
        lines.append("")

    if sdlc:
        lines.append("[In progress]")
        for e in sdlc:
            summary = f" — {e.summary}" if e.summary else ""
            lines.append(f"- {e.name} [{e.status}]{summary}")
        lines.append("")

    if active:
        lines.append("[Active]")
        for e in active:
            summary = f" — {e.summary}" if e.summary else ""
            lines.append(f"- {e.name}{summary}")
        lines.append("")

    if brainstorming:
        lines.append("[Brainstorming]")
        lines.append("- " + ", ".join(e.name for e in brainstorming))
        lines.append("")


def _render_todos(lines: list[str], todos: TodoSummary) -> None:
    remaining = todos.count - len(todos.items)
    lines.append(f"**Open todos (undone):** {todos.count} items")
    for item in todos.items:
        lines.append(f"  - {item}")
    if remaining > 0:
        lines.append(f"  + {remaining} more")
    lines.append("")


# ── Public entry point ────────────────────────────────────────────────────────


def build_context_block(
    notebook_path: Path,
    short: bool = False,
    include_brainstorming: bool = False,
    project_filter: str | None = None,
    now: datetime | None = None,
) -> str:
    projects = parse_projects(notebook_path, include_brainstorming=include_brainstorming)
    if project_filter:
        projects = [p for p in projects if p.name.lower() == project_filter.lower()]
    todos = parse_todos(notebook_path)
    inbox = parse_inbox(notebook_path)
    last_daily = last_daily_note(notebook_path)
    return render_context_block(projects, todos, inbox, last_daily, now=now, short=short)
