"""Tests for the rewake module — shared asyncRewake emitter."""

from __future__ import annotations

import json

from aya.rewake import emit


class TestEmit:
    def test_writes_json_to_stdout(self, capsys):
        emit("CI failed on PR #42")
        out = capsys.readouterr().out
        payload = json.loads(out.strip())
        assert payload["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
        assert payload["hookSpecificOutput"]["additionalContext"] == "CI failed on PR #42"

    def test_custom_event_name(self, capsys):
        emit("session started", event_name="SessionStart")
        out = capsys.readouterr().out
        payload = json.loads(out.strip())
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"

    def test_output_ends_with_newline(self, capsys):
        emit("test")
        out = capsys.readouterr().out
        assert out.endswith("\n")
