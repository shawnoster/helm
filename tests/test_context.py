"""Tests for aya.context — parsers and renderer."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aya.cli import app
from aya.context import (
    InboxSummary,
    ProjectEntry,
    TodoSummary,
    last_daily_note,
    parse_inbox,
    parse_projects,
    parse_todos,
    render_context_block,
)

runner = CliRunner()


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_project(tmp_path: Path, name: str, status: str, summary: str = "") -> None:
    content = f"# {name}\n\n**Status:** {status}\n"
    if summary:
        content += f"\n{summary}\n"
    (tmp_path / f"{name}.md").write_text(content)


def _make_notebook(tmp_path: Path) -> Path:
    nb = tmp_path / "notebook"
    (nb / "projects").mkdir(parents=True)
    (nb / "daily").mkdir()
    return nb


# ── parse_projects ────────────────────────────────────────────────────────────


def test_parse_projects_filters_shelved(tmp_path: Path) -> None:
    nb = _make_notebook(tmp_path)
    _make_project(nb / "projects", "aya", "Active", "A great tool")
    _make_project(nb / "projects", "veilwood", "Shelved")
    entries = parse_projects(nb)
    assert len(entries) == 1
    assert entries[0].name == "aya"


def test_parse_projects_filters_done(tmp_path: Path) -> None:
    nb = _make_notebook(tmp_path)
    _make_project(nb / "projects", "aya", "Active")
    _make_project(nb / "projects", "oldthing", "Done")
    entries = parse_projects(nb)
    assert all(e.name != "oldthing" for e in entries)


def test_parse_projects_excludes_brainstorming_by_default(tmp_path: Path) -> None:
    nb = _make_notebook(tmp_path)
    _make_project(nb / "projects", "aya", "Active")
    _make_project(nb / "projects", "wild-idea", "Brainstorming")
    entries = parse_projects(nb)
    assert all(e.name != "wild-idea" for e in entries)


def test_parse_projects_includes_brainstorming_when_flag_set(tmp_path: Path) -> None:
    nb = _make_notebook(tmp_path)
    _make_project(nb / "projects", "wild-idea", "Brainstorming")
    entries = parse_projects(nb, include_brainstorming=True)
    assert any(e.name == "wild-idea" for e in entries)
    assert entries[0].project_type == "brainstorming"


def test_parse_projects_status_with_suffix(tmp_path: Path) -> None:
    nb = _make_notebook(tmp_path)
    _make_project(nb / "projects", "myproject", "Active — Phase 3 complete")
    entries = parse_projects(nb)
    assert entries[0].status == "Active"
    assert entries[0].project_type == "active"


def test_parse_projects_sdlc_type(tmp_path: Path) -> None:
    nb = _make_notebook(tmp_path)
    _make_project(nb / "projects", "myproject", "Planning")
    entries = parse_projects(nb)
    assert entries[0].project_type == "sdlc"
    assert entries[0].status == "Planning"


def test_parse_projects_blocked_type(tmp_path: Path) -> None:
    nb = _make_notebook(tmp_path)
    _make_project(nb / "projects", "broken", "Blocked — waiting on infra")
    entries = parse_projects(nb)
    assert entries[0].project_type == "blocked"
    assert entries[0].status == "Blocked"


def test_parse_projects_sort_order(tmp_path: Path) -> None:
    nb = _make_notebook(tmp_path)
    _make_project(nb / "projects", "zzz-active", "Active")
    _make_project(nb / "projects", "aaa-planning", "Planning")
    _make_project(nb / "projects", "aaa-blocked", "Blocked")
    entries = parse_projects(nb)
    types = [e.project_type for e in entries]
    # blocked must come before sdlc, sdlc before active
    assert types.index("blocked") < types.index("sdlc")
    assert types.index("sdlc") < types.index("active")


def test_parse_projects_sdlc_pipeline_order(tmp_path: Path) -> None:
    nb = _make_notebook(tmp_path)
    _make_project(nb / "projects", "b-impl", "Implementation")
    _make_project(nb / "projects", "a-disc", "Discovery")
    _make_project(nb / "projects", "c-plan", "Planning")
    entries = parse_projects(nb)
    names = [e.name for e in entries]
    assert names.index("a-disc") < names.index("c-plan")
    assert names.index("c-plan") < names.index("b-impl")


def test_parse_projects_extracts_summary(tmp_path: Path) -> None:
    nb = _make_notebook(tmp_path)
    _make_project(nb / "projects", "aya", "Active", "Interface layer for a personal AI assistant.")
    entries = parse_projects(nb)
    assert "Interface layer" in entries[0].summary


# ── parse_todos ───────────────────────────────────────────────────────────────


def test_parse_todos_counts_unchecked_only(tmp_path: Path) -> None:
    nb = _make_notebook(tmp_path)
    (nb / "todos.md").write_text("# Todos\n\n- [ ] Do this\n- [x] Done already\n- [ ] Do that\n")
    result = parse_todos(nb)
    assert result.count == 2
    assert result.items == ["Do this", "Do that"]


def test_parse_todos_limit(tmp_path: Path) -> None:
    nb = _make_notebook(tmp_path)
    items = "\n".join(f"- [ ] Item {i}" for i in range(10))
    (nb / "todos.md").write_text(f"# Todos\n\n{items}\n")
    result = parse_todos(nb, limit=5)
    assert result.count == 10
    assert len(result.items) == 5


def test_parse_todos_missing_file(tmp_path: Path) -> None:
    nb = _make_notebook(tmp_path)
    result = parse_todos(nb)
    assert result.count == 0
    assert result.items == []


# ── parse_inbox ───────────────────────────────────────────────────────────────


def test_parse_inbox_empty_when_all_struck(tmp_path: Path) -> None:
    nb = _make_notebook(tmp_path)
    (nb / "inbox.md").write_text("# Inbox\n\n---\n\n- ~~routed item~~\n")
    result = parse_inbox(nb)
    assert result.count == 0


def test_parse_inbox_counts_active_items(tmp_path: Path) -> None:
    nb = _make_notebook(tmp_path)
    (nb / "inbox.md").write_text(
        "# Inbox\n\n---\n\n- ~~routed~~\n- Active capture item\n- Another item\n"
    )
    result = parse_inbox(nb)
    assert result.count == 2


def test_parse_inbox_missing_file(tmp_path: Path) -> None:
    nb = _make_notebook(tmp_path)
    result = parse_inbox(nb)
    assert result.count == 0


# ── last_daily_note ───────────────────────────────────────────────────────────


def test_last_daily_note_returns_most_recent(tmp_path: Path) -> None:
    nb = _make_notebook(tmp_path)
    (nb / "daily" / "2026-03-29.md").write_text("")
    (nb / "daily" / "2026-03-31.md").write_text("")
    (nb / "daily" / "2026-03-30.md").write_text("")
    assert last_daily_note(nb) == "2026-03-31"


def test_last_daily_note_none_when_empty(tmp_path: Path) -> None:
    nb = _make_notebook(tmp_path)
    assert last_daily_note(nb) is None


# ── render_context_block ──────────────────────────────────────────────────────

_NOW = datetime(2026, 3, 31, 10, 0, 0, tzinfo=UTC)


def test_render_full_output_structure(tmp_path: Path) -> None:
    projects = [
        ProjectEntry("aya", "Planning", "sdlc", "Interface layer"),
        ProjectEntry("babar", "Active", "active", "Home server"),
    ]
    todos = TodoSummary(count=7, items=["Do this", "Do that", "And this", "Also that", "One more"])
    inbox = InboxSummary(count=0)
    output = render_context_block(projects, todos, inbox, "2026-03-31", now=_NOW)

    assert "## Session context — 2026-03-31 (Tuesday)" in output
    assert "[In progress]" in output
    assert "aya [Planning]" in output
    assert "[Active]" in output
    assert "babar" in output
    assert "**Open todos (undone):** 7 items" in output
    assert "  - Do this" in output
    assert "+ 2 more" in output
    assert "**Inbox:** empty" in output
    assert "**Last daily note:** 2026-03-31" in output


def test_render_short_format(tmp_path: Path) -> None:
    projects = [
        ProjectEntry("aya", "Planning", "sdlc", "Interface layer"),
        ProjectEntry("babar", "Active", "active", "Home server"),
    ]
    todos = TodoSummary(count=3, items=["Item"])
    inbox = InboxSummary(count=2)
    output = render_context_block(projects, todos, inbox, "2026-03-31", now=_NOW, short=True)

    assert "**Projects:** aya [Planning], babar [Active]" in output
    assert "**Todos:** 3 open" in output
    assert "**Inbox:** 2 items" in output
    assert "[In progress]" not in output


def test_render_blocked_shown_first(tmp_path: Path) -> None:
    projects = [
        ProjectEntry("broken", "Blocked", "blocked", "Waiting on infra"),
        ProjectEntry("aya", "Active", "active", "Tool"),
    ]
    todos = TodoSummary(count=0)
    inbox = InboxSummary(count=0)
    output = render_context_block(projects, todos, inbox, None, now=_NOW)

    assert output.index("[BLOCKED]") < output.index("[Active]")


def test_render_brainstorming_names_only(tmp_path: Path) -> None:
    projects = [
        ProjectEntry("wild-idea", "Brainstorming", "brainstorming", "Something cool"),
    ]
    todos = TodoSummary(count=0)
    inbox = InboxSummary(count=0)
    output = render_context_block(projects, todos, inbox, None, now=_NOW)

    assert "[Brainstorming]" in output
    assert "Something cool" not in output  # summary omitted
    assert "wild-idea" in output


# ── CLI integration ───────────────────────────────────────────────────────────


def test_context_cmd_no_notebook_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aya.cli.get_notebook_path", lambda: None)
    result = runner.invoke(app, ["context"])
    assert result.exit_code == 1
    assert "notebook_path not set" in result.output


def test_context_cmd_renders_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nb = _make_notebook(tmp_path)
    _make_project(nb / "projects", "aya", "Active", "A tool")
    (nb / "todos.md").write_text("- [ ] Do something\n")
    (nb / "inbox.md").write_text("")
    (nb / "daily" / "2026-03-31.md").write_text("")

    monkeypatch.setattr("aya.cli.get_notebook_path", lambda: nb)

    result = runner.invoke(app, ["context"])
    assert result.exit_code == 0
    assert "Session context" in result.output
    assert "aya" in result.output
