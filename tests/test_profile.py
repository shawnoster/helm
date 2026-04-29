"""Tests for aya.profile — profile initialization, name rotation, and activity."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from aya.profile import (
    NAME_CANDIDATES,
    REEVALUATION_DAYS,
    _activity_entries_last_days,
    _activity_themes,
    _default_profile,
    _iso_z,
    _name_from_activity,
    _parse_iso,
    _rotated_name,
    ensure_profile,
)

# ── _iso_z ───────────────────────────────────────────────────────────────────


class TestIsoZ:
    def test_replaces_plus_offset_with_z(self):
        dt = datetime(2026, 1, 15, 10, 30, 45, tzinfo=UTC)
        result = _iso_z(dt)
        assert result.endswith("Z")
        assert "+00:00" not in result

    def test_strips_microseconds(self):
        dt = datetime(2026, 1, 15, 10, 30, 45, 123456, tzinfo=UTC)
        result = _iso_z(dt)
        assert "123456" not in result
        assert result == "2026-01-15T10:30:45Z"

    def test_format_structure(self):
        dt = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
        result = _iso_z(dt)
        assert result == "2026-06-01T00:00:00Z"


# ── _parse_iso ───────────────────────────────────────────────────────────────


class TestParseIso:
    def test_none_returns_none(self):
        assert _parse_iso(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_iso("") is None

    def test_valid_z_suffix(self):
        result = _parse_iso("2026-01-15T10:30:45Z")
        assert result is not None
        assert result.year == 2026
        assert result.month == 1
        assert result.day == 15

    def test_valid_offset(self):
        result = _parse_iso("2026-01-15T10:30:45+00:00")
        assert result is not None

    def test_invalid_string_returns_none(self):
        assert _parse_iso("not-a-date") is None

    def test_malformed_returns_none(self):
        assert _parse_iso("2026-13-45") is None


# ── _rotated_name ────────────────────────────────────────────────────────────


class TestRotatedName:
    def test_none_returns_first(self):
        assert _rotated_name(None) == NAME_CANDIDATES[0]

    def test_unknown_name_returns_first(self):
        assert _rotated_name("Unknown GSV Ship") == NAME_CANDIDATES[0]

    def test_rotates_to_next(self):
        first = NAME_CANDIDATES[0]
        second = NAME_CANDIDATES[1]
        assert _rotated_name(first) == second

    def test_wraps_around(self):
        last = NAME_CANDIDATES[-1]
        first = NAME_CANDIDATES[0]
        assert _rotated_name(last) == first

    def test_all_candidates_rotate(self):
        """Every candidate rotates to the next (and wraps)."""
        for i, name in enumerate(NAME_CANDIDATES):
            expected = NAME_CANDIDATES[(i + 1) % len(NAME_CANDIDATES)]
            assert _rotated_name(name) == expected


# ── _activity_entries_last_days ──────────────────────────────────────────────


class TestActivityEntriesLastDays:
    def test_missing_file_returns_empty(self, tmp_path):
        missing = tmp_path / "no_such_file.md"
        now = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
        assert _activity_entries_last_days(missing, now) == []

    def test_parses_entries_within_window(self, tmp_path):
        log = tmp_path / "done.md"
        now = datetime(2026, 4, 3, 12, 0, tzinfo=UTC)
        log.write_text(
            "## 2026-04-03\n- reviewed PR\n- updated docs\n## 2026-04-01\n- wrote script\n"
        )
        entries = _activity_entries_last_days(log, now)
        assert "reviewed PR" in entries
        assert "updated docs" in entries
        assert "wrote script" in entries

    def test_excludes_old_entries(self, tmp_path):
        log = tmp_path / "done.md"
        now = datetime(2026, 4, 3, 12, 0, tzinfo=UTC)
        log.write_text("## 2026-03-01\n- old task\n## 2026-04-03\n- new task\n")
        entries = _activity_entries_last_days(log, now)
        assert "new task" in entries
        assert "old task" not in entries

    def test_invalid_section_header_skipped(self, tmp_path):
        log = tmp_path / "done.md"
        now = datetime(2026, 4, 3, 12, 0, tzinfo=UTC)
        log.write_text("## not-a-date\n- should be skipped\n## 2026-04-03\n- valid task\n")
        entries = _activity_entries_last_days(log, now)
        assert "should be skipped" not in entries
        assert "valid task" in entries

    def test_empty_file_returns_empty(self, tmp_path):
        log = tmp_path / "done.md"
        log.write_text("")
        now = datetime(2026, 4, 3, 12, 0, tzinfo=UTC)
        assert _activity_entries_last_days(log, now) == []

    def test_non_dash_lines_not_included(self, tmp_path):
        log = tmp_path / "done.md"
        now = datetime(2026, 4, 3, 12, 0, tzinfo=UTC)
        log.write_text("## 2026-04-03\nplain text\n  * bullet\n- valid\n")
        entries = _activity_entries_last_days(log, now)
        assert "plain text" not in entries
        assert entries == ["valid"]


# ── _activity_themes ─────────────────────────────────────────────────────────


class TestActivityThemes:
    def test_empty_entries_zero_scores(self):
        scores = _activity_themes([])
        assert all(v == 0 for v in scores.values())

    def test_workflow_keyword_detected(self):
        scores = _activity_themes(["updated workflow config", "setup new ritual"])
        assert scores["workflow"] >= 2

    def test_docs_keyword_detected(self):
        scores = _activity_themes(["wrote readme", "updated docs", "confluence page"])
        assert scores["docs"] >= 2

    def test_multiple_themes(self):
        scores = _activity_themes(["wrote docs", "created automation script"])
        assert scores["docs"] >= 1
        assert scores["automation"] >= 1

    def test_case_insensitive(self):
        scores = _activity_themes(["WORKFLOW review", "Memory update"])
        assert scores["workflow"] >= 1
        assert scores["memory"] >= 1

    def test_architecture_detected(self):
        scores = _activity_themes(["updated c4 diagram", "domain architecture review"])
        assert scores["architecture"] >= 2


# ── _name_from_activity ──────────────────────────────────────────────────────


class TestNameFromActivity:
    def test_no_entries_uses_ordinal(self):
        now = datetime(2026, 4, 1, tzinfo=UTC)
        name = _name_from_activity(now, [])
        assert name in NAME_CANDIDATES

    def test_no_matching_themes_uses_ordinal(self):
        now = datetime(2026, 4, 1, tzinfo=UTC)
        entries = ["blah blah nothing matches", "random words"]
        name = _name_from_activity(now, entries)
        assert name in NAME_CANDIDATES

    def test_generates_gsv_name_from_themes(self):
        now = datetime(2026, 4, 1, tzinfo=UTC)
        entries = ["updated workflow", "setup ritual", "launch new workflow"]
        name = _name_from_activity(now, entries)
        assert name.startswith("GSV ")

    def test_single_theme_uses_same_for_secondary(self):
        now = datetime(2026, 4, 1, tzinfo=UTC)
        entries = ["wrote docs", "updated readme", "confluence guide"]
        name = _name_from_activity(now, entries)
        assert name.startswith("GSV ")

    def test_two_distinct_themes(self):
        now = datetime(2026, 4, 1, tzinfo=UTC)
        entries = ["docs readme", "workflow ritual", "automation script"]
        name = _name_from_activity(now, entries)
        assert name.startswith("GSV ")


# ── _default_profile ─────────────────────────────────────────────────────────


class TestDefaultProfile:
    def test_has_required_keys(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        p = _default_profile(now)
        assert "alias" in p
        assert "ship_mind_name" in p
        assert "name_last_evaluated_at" in p
        assert "name_next_reevaluation_at" in p
        assert "persona" in p
        assert "movement_reminders" in p

    def test_next_eval_is_reevaluation_days_out(self):
        now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        p = _default_profile(now)
        next_eval = _parse_iso(p["name_next_reevaluation_at"])
        assert next_eval is not None
        expected = now + timedelta(days=REEVALUATION_DAYS)
        # Compare without sub-second precision
        assert abs((next_eval - expected).total_seconds()) < 2

    def test_default_ship_name_is_first_candidate(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        p = _default_profile(now)
        assert p["ship_mind_name"] == NAME_CANDIDATES[0]


# ── ensure_profile ───────────────────────────────────────────────────────────


class TestEnsureProfile:
    def test_creates_profile_when_missing(self, tmp_path):
        path = tmp_path / "profile.json"
        now = datetime(2026, 1, 1, tzinfo=UTC)
        profile = ensure_profile(path, now=now)
        assert path.exists()
        assert profile["alias"] == "Assistant"
        assert profile["ship_mind_name"] in NAME_CANDIDATES

    def test_persists_to_disk(self, tmp_path):
        path = tmp_path / "profile.json"
        now = datetime(2026, 1, 1, tzinfo=UTC)
        ensure_profile(path, now=now)
        data = json.loads(path.read_text())
        assert "ship_mind_name" in data

    def test_loads_existing_profile(self, tmp_path):
        path = tmp_path / "profile.json"
        # Write an existing profile with a custom alias
        future = (datetime.now(UTC) + timedelta(days=10)).replace(microsecond=0)
        existing = {
            "alias": "Custom Alias",
            "ship_mind_name": NAME_CANDIDATES[1],
            "name_last_evaluated_at": _iso_z(datetime.now(UTC)),
            "name_next_reevaluation_at": _iso_z(future),
        }
        path.write_text(json.dumps(existing))
        now = datetime(2026, 1, 1, tzinfo=UTC)
        profile = ensure_profile(path, now=now)
        assert profile["alias"] == "Custom Alias"

    def test_invalid_json_falls_back_to_default(self, tmp_path):
        path = tmp_path / "profile.json"
        path.write_text("{not valid json}")
        now = datetime(2026, 1, 1, tzinfo=UTC)
        profile = ensure_profile(path, now=now)
        assert "ship_mind_name" in profile

    def test_reevaluation_when_past_due(self, tmp_path):
        path = tmp_path / "profile.json"
        old_time = datetime(2025, 1, 1, tzinfo=UTC)
        existing = {
            "alias": "Assistant",
            "ship_mind_name": NAME_CANDIDATES[0],
            "name_last_evaluated_at": _iso_z(old_time),
            "name_next_reevaluation_at": _iso_z(old_time + timedelta(days=REEVALUATION_DAYS)),
        }
        path.write_text(json.dumps(existing))
        now = datetime(2026, 4, 1, tzinfo=UTC)
        profile = ensure_profile(path, now=now)
        # Should have been reevaluated; next eval should be ~3 days from now
        next_eval = _parse_iso(profile["name_next_reevaluation_at"])
        assert next_eval is not None
        expected = now + timedelta(days=REEVALUATION_DAYS)
        assert abs((next_eval - expected).total_seconds()) < 2

    def test_no_reevaluation_when_not_due(self, tmp_path):
        path = tmp_path / "profile.json"
        now = datetime(2026, 4, 1, tzinfo=UTC)
        future = now + timedelta(days=10)
        existing = {
            "alias": "Assistant",
            "ship_mind_name": NAME_CANDIDATES[2],
            "name_last_evaluated_at": _iso_z(now - timedelta(days=1)),
            "name_next_reevaluation_at": _iso_z(future),
        }
        path.write_text(json.dumps(existing))
        profile = ensure_profile(path, now=now)
        # Name should be unchanged since it's not due yet
        assert profile["ship_mind_name"] == NAME_CANDIDATES[2]

    def test_none_next_eval_preserves_existing_name(self, tmp_path):
        path = tmp_path / "profile.json"
        existing = {
            "alias": "Assistant",
            "ship_mind_name": NAME_CANDIDATES[3],
        }
        path.write_text(json.dumps(existing))
        now = datetime(2026, 4, 1, tzinfo=UTC)
        profile = ensure_profile(path, now=now)
        # When next_eval is None, current name is preserved
        assert profile["ship_mind_name"] == NAME_CANDIDATES[3]

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "nested" / "deep" / "profile.json"
        now = datetime(2026, 1, 1, tzinfo=UTC)
        ensure_profile(path, now=now)
        assert path.exists()

    def test_reevaluation_rotates_if_name_would_repeat(self, tmp_path):
        """When activity generates the same name as current, rotation kicks in."""
        path = tmp_path / "profile.json"
        old_time = datetime(2025, 1, 1, tzinfo=UTC)
        # Use a 'now' ordinal that maps to NAME_CANDIDATES[0] with empty activity
        # so that _name_from_activity will return NAME_CANDIDATES[0]
        # Pick a 'now' where now.toordinal() % 5 == 0
        now = datetime(2026, 4, 1, tzinfo=UTC)
        ordinal = now.toordinal()
        expected_by_ordinal = NAME_CANDIDATES[ordinal % len(NAME_CANDIDATES)]
        existing = {
            "alias": "Assistant",
            "ship_mind_name": expected_by_ordinal,
            "name_last_evaluated_at": _iso_z(old_time),
            "name_next_reevaluation_at": _iso_z(old_time + timedelta(days=REEVALUATION_DAYS)),
        }
        path.write_text(json.dumps(existing))
        profile = ensure_profile(path, now=now)
        # Name must have changed (rotated away from the repeat)
        assert profile["ship_mind_name"] != expected_by_ordinal

    def test_profile_returns_dict_with_ship_mind_name(self, tmp_path):
        """ensure_profile always returns a dict with ship_mind_name."""
        path = tmp_path / "profile.json"
        now = datetime(2026, 1, 1, tzinfo=UTC)
        profile = ensure_profile(path, now=now)
        assert isinstance(profile, dict)
        assert "ship_mind_name" in profile

    def test_null_next_eval_in_existing_profile_triggers_reset(self, tmp_path):
        """When loaded profile has name_next_reevaluation_at=null, it's treated as None."""
        path = tmp_path / "profile.json"
        # Write existing profile with explicit null next eval
        existing = {
            "alias": "Assistant",
            "ship_mind_name": NAME_CANDIDATES[1],
            "name_last_evaluated_at": None,
            "name_next_reevaluation_at": None,
        }
        path.write_text(json.dumps(existing))
        now = datetime(2026, 4, 1, tzinfo=UTC)
        profile = ensure_profile(path, now=now)
        # name_next_reevaluation_at should now be set properly
        assert profile.get("name_next_reevaluation_at") is not None
        assert profile["name_next_reevaluation_at"].endswith("Z")

    def test_null_current_name_uses_first_candidate_when_next_eval_is_none(self, tmp_path):
        """When current name is not a string and next_eval is None, NAME_CANDIDATES[0] is used."""
        path = tmp_path / "profile.json"
        existing = {
            "alias": "Assistant",
            "ship_mind_name": None,  # Not a string
            "name_next_reevaluation_at": None,
        }
        path.write_text(json.dumps(existing))
        now = datetime(2026, 4, 1, tzinfo=UTC)
        profile = ensure_profile(path, now=now)
        assert profile["ship_mind_name"] == NAME_CANDIDATES[0]
