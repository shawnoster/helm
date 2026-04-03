"""Tests for workspace configuration — get_notebook_path env var support."""

from __future__ import annotations

import json
from pathlib import Path

from aya.config import get_notebook_path


class TestGetNotebookPath:
    def test_returns_none_when_unset(self, tmp_path):
        config = tmp_path / "config.json"
        config.write_text("{}")
        assert get_notebook_path(config) is None

    def test_reads_from_config(self, tmp_path):
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"notebook_path": "/from/config"}))
        result = get_notebook_path(config)
        assert result == Path("/from/config")

    def test_env_var_takes_precedence(self, tmp_path, monkeypatch):
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"notebook_path": "/from/config"}))
        monkeypatch.setenv("AYA_NOTEBOOK_PATH", "/from/env")
        result = get_notebook_path(config)
        assert result == Path("/from/env")

    def test_env_var_strips_whitespace(self, tmp_path, monkeypatch):
        config = tmp_path / "config.json"
        config.write_text("{}")
        monkeypatch.setenv("AYA_NOTEBOOK_PATH", "  /from/env  ")
        result = get_notebook_path(config)
        assert result == Path("/from/env")

    def test_empty_env_var_falls_through(self, tmp_path, monkeypatch):
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"notebook_path": "/from/config"}))
        monkeypatch.setenv("AYA_NOTEBOOK_PATH", "  ")
        result = get_notebook_path(config)
        assert result == Path("/from/config")

    def test_expands_tilde_from_env(self, tmp_path, monkeypatch):
        config = tmp_path / "config.json"
        config.write_text("{}")
        monkeypatch.setenv("AYA_NOTEBOOK_PATH", "~/notebook")
        result = get_notebook_path(config)
        assert result == Path.home() / "notebook"
