"""Tests for aya.credentials — the system ACK surface."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from aya.credentials import (
    CANONICAL_SERVICES,
    CredentialsReport,
    check_credentials,
    check_service,
)

# ── check_service ─────────────────────────────────────────────────────────────


class TestCheckService:
    def test_all_vars_set_is_lit(self) -> None:
        env = {"A": "value-a", "B": "value-b"}
        result = check_service("svc", ["A", "B"], env=env)
        assert result.state == "lit"
        assert result.set_vars == ["A", "B"]
        assert result.missing == []

    def test_no_vars_set_is_dark(self) -> None:
        result = check_service("svc", ["A", "B"], env={})
        assert result.state == "dark"
        assert result.set_vars == []
        assert result.missing == ["A", "B"]

    def test_some_vars_set_is_partial(self) -> None:
        env = {"A": "value-a"}
        result = check_service("svc", ["A", "B", "C"], env=env)
        assert result.state == "partial"
        assert result.set_vars == ["A"]
        assert result.missing == ["B", "C"]

    def test_empty_string_counts_as_unset(self) -> None:
        """`export A=` leaves A set to an empty string. We treat that
        as unset — matches common shell semantics and guards against
        the "I thought I cleared it" case."""
        env = {"A": "", "B": "value-b"}
        result = check_service("svc", ["A", "B"], env=env)
        assert result.state == "partial"
        assert result.set_vars == ["B"]
        assert result.missing == ["A"]

    def test_whitespace_only_counts_as_unset(self) -> None:
        """Tokens accidentally set to whitespace are not credentials."""
        env = {"A": "   ", "B": "\t\n"}
        result = check_service("svc", ["A", "B"], env=env)
        assert result.state == "dark"
        assert result.set_vars == []

    def test_single_var_service(self) -> None:
        result = check_service("github", ["GITHUB_TOKEN"], env={"GITHUB_TOKEN": "ghp_xxx"})
        assert result.state == "lit"
        assert result.required == ["GITHUB_TOKEN"]

    def test_required_list_is_copied_not_aliased(self) -> None:
        """Mutating the returned required list must not affect the caller's list."""
        original = ["A", "B"]
        result = check_service("svc", original, env={"A": "v"})
        result.required.append("C")
        assert original == ["A", "B"]

    def test_defaults_to_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_CRED_VAR_42", "set")
        result = check_service("test", ["TEST_CRED_VAR_42"])
        assert result.state == "lit"

    def test_returns_a_frozen_dataclass(self) -> None:
        """ServiceCredential is frozen — catches accidental mutation."""
        result = check_service("svc", ["A"], env={"A": "v"})
        with pytest.raises(FrozenInstanceError):
            result.state = "dark"  # type: ignore[misc]

    def test_empty_required_list_is_vacuously_lit(self) -> None:
        """A service with zero required env vars has nothing to check —
        it's trivially ready. Without this guard the naive logic would
        report it as 'dark' because set_vars is empty, which is wrong."""
        result = check_service("svc", [], env={})
        assert result.state == "lit"
        assert result.required == []
        assert result.set_vars == []
        assert result.missing == []


# ── check_credentials ─────────────────────────────────────────────────────────


class TestCheckCredentials:
    def test_all_services_dark_by_default(self) -> None:
        """With an empty env, every service in the override map is dark."""
        services = {"github": ["GITHUB_TOKEN"], "npm": ["NPM_TOKEN"]}
        report = check_credentials(services=services, env={})
        assert report.lit == 0
        assert report.partial == 0
        assert report.dark == 2
        assert len(report.services) == 2
        assert all(s.state == "dark" for s in report.services)

    def test_mixed_states(self) -> None:
        """Exercise the lit/partial/dark counting across a mixed env."""
        services = {
            "github": ["GITHUB_TOKEN"],
            "atlassian": ["ATLASSIAN_API_TOKEN", "ATLASSIAN_EMAIL", "ATLASSIAN_SERVER_URL"],
            "npm": ["NPM_TOKEN"],
            "datadog": ["DATADOG_API_KEY", "DATADOG_APP_KEY"],
        }
        env = {
            "GITHUB_TOKEN": "ghp_xxx",  # lit
            "ATLASSIAN_API_TOKEN": "tok",  # partial: 1/3
            # npm → dark
            "DATADOG_API_KEY": "k",
            "DATADOG_APP_KEY": "a",  # lit
        }
        report = check_credentials(services=services, env=env)
        assert report.lit == 2
        assert report.partial == 1
        assert report.dark == 1

        states = {s.name: s.state for s in report.services}
        assert states == {
            "github": "lit",
            "atlassian": "partial",
            "npm": "dark",
            "datadog": "lit",
        }

    def test_services_are_ordered_by_dict_iteration_order(self) -> None:
        """Stable order matters for deterministic output."""
        services = {"b": ["B"], "a": ["A"], "c": ["C"]}
        report = check_credentials(services=services, env={})
        assert [s.name for s in report.services] == ["b", "a", "c"]

    def test_partial_service_missing_list_preserves_required_order(self) -> None:
        services = {"atlassian": ["ATLASSIAN_API_TOKEN", "ATLASSIAN_EMAIL", "ATLASSIAN_SERVER_URL"]}
        env = {"ATLASSIAN_EMAIL": "me@example.com"}
        report = check_credentials(services=services, env=env)
        service = report.services[0]
        assert service.state == "partial"
        assert service.set_vars == ["ATLASSIAN_EMAIL"]
        assert service.missing == ["ATLASSIAN_API_TOKEN", "ATLASSIAN_SERVER_URL"]

    def test_defaults_to_canonical_services_and_os_environ(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without overrides, uses the real catalog and the real env."""
        # Clear every canonical var so we start from a known state
        for vars_ in CANONICAL_SERVICES.values():
            for var in vars_:
                monkeypatch.delenv(var, raising=False)

        report = check_credentials()
        assert len(report.services) == len(CANONICAL_SERVICES)
        # All services should be dark now
        assert report.lit == 0
        assert report.partial == 0
        assert report.dark == len(CANONICAL_SERVICES)

    def test_report_counts_match_services(self) -> None:
        services = {
            "a": ["A"],
            "b": ["B1", "B2"],
            "c": ["C"],
        }
        env = {"A": "v", "B1": "v"}  # a=lit, b=partial, c=dark
        report = check_credentials(services=services, env=env)
        assert report.lit + report.partial + report.dark == len(services) == len(report.services)


# ── Catalog sanity ────────────────────────────────────────────────────────────


class TestCanonicalServices:
    def test_catalog_is_non_empty(self) -> None:
        assert len(CANONICAL_SERVICES) > 0

    def test_every_service_has_at_least_one_required_var(self) -> None:
        for name, vars_ in CANONICAL_SERVICES.items():
            assert len(vars_) >= 1, f"Service {name!r} has no required env vars"

    def test_every_required_var_name_is_uppercase_with_underscores(self) -> None:
        """Convention check: env var names follow SCREAMING_SNAKE_CASE."""
        import re

        pattern = re.compile(r"^[A-Z][A-Z0-9_]*$")
        for name, vars_ in CANONICAL_SERVICES.items():
            for var in vars_:
                assert pattern.match(var), f"Service {name!r} has non-conventional var name {var!r}"

    def test_canonical_services_include_common_integrations(self) -> None:
        """Lock-in: these services are expected to be in the catalog.
        If you remove one, update this test deliberately — it's a signal
        to whoever reviews the PR that the catalog shrunk."""
        required = {"github", "atlassian", "datadog", "npm"}
        assert required.issubset(CANONICAL_SERVICES.keys())

    def test_no_duplicate_var_names_across_services(self) -> None:
        """A var name should only appear once in the catalog. If two
        services both claim ATLASSIAN_API_TOKEN, that's a bug."""
        seen: dict[str, str] = {}
        for service_name, vars_ in CANONICAL_SERVICES.items():
            for var in vars_:
                if var in seen:
                    raise AssertionError(
                        f"Var {var!r} claimed by both {seen[var]!r} and {service_name!r}"
                    )
                seen[var] = service_name


# ── Integration: aya status output includes credentials ──────────────────────


class TestStatusIntegration:
    """Smoke tests that the credentials report flows through aya status."""

    def test_gather_status_includes_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from aya.status import _gather_status

        # Clear any user env so the test is deterministic
        for vars_ in CANONICAL_SERVICES.values():
            for var in vars_:
                monkeypatch.delenv(var, raising=False)

        data = _gather_status()
        assert "credentials" in data
        report = data["credentials"]
        assert isinstance(report, CredentialsReport)
        assert report.dark == len(CANONICAL_SERVICES)

    def test_json_output_exposes_credentials_section(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        from aya.status import run_status

        # Set one var so the output has non-trivial content
        for vars_ in CANONICAL_SERVICES.values():
            for var in vars_:
                monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

        run_status(format_="json")
        captured = capsys.readouterr()
        payload = json.loads(captured.out)

        assert "credentials" in payload
        cred = payload["credentials"]
        assert "services" in cred
        assert "github" in cred["services"]
        assert cred["services"]["github"]["state"] == "lit"
        # Other services should be dark
        assert cred["dark"] == len(CANONICAL_SERVICES) - 1
        assert cred["lit"] == 1

    def test_text_output_mentions_credentials(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from aya.status import run_status

        for vars_ in CANONICAL_SERVICES.values():
            for var in vars_:
                monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

        run_status(format_="text")
        out = capsys.readouterr().out
        assert "Credentials" in out
        assert "github" in out
