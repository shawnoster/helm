"""Credential readiness checks for common service integrations.

The "system ACK" surface for ``aya status``: for each canonical service
(GitHub, Atlassian/Jira, Datadog, npm, etc.), check whether the env
vars required to talk to it are actually set in the current process
environment. Report per-service state (lit / partial / dark) so the
Ship Mind can glance and know which kingdoms are reachable.

This module only checks *presence* of env vars. It does NOT read or
validate their contents — no API calls, no secret lookups. That
preserves three properties:

  1. Fast (a few dict lookups, no network).
  2. Safe (no values leave the process, ever).
  3. Side-effect free (the check is idempotent and silent).

The canonical service list is hardcoded because these are the services
aya commonly integrates with. Adding a new service is a one-line edit
to ``CANONICAL_SERVICES``; user-level customization can come later via
``~/.aya/config.json`` if the need arises (YAGNI for now).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

# ── Canonical services ────────────────────────────────────────────────────────
#
# Each entry maps a service name to the list of environment variables
# that must be set for that service to be "lit". All vars are required
# (AND, not OR). If you need OR semantics (e.g. GITHUB_TOKEN *or*
# GITHUB_PERSONAL_ACCESS_TOKEN), add both canonical names here and let
# the check report one as missing — the user can set either and at
# least one of the two paths will show "lit" once user-level overrides
# land.

CANONICAL_SERVICES: dict[str, list[str]] = {
    "github": ["GITHUB_TOKEN"],
    "atlassian": [
        "ATLASSIAN_API_TOKEN",
        "ATLASSIAN_EMAIL",
        "ATLASSIAN_SERVER_URL",
    ],
    "datadog": ["DATADOG_API_KEY", "DATADOG_APP_KEY"],
    "gitea": ["GITEA_TOKEN"],
    "npm": ["NPM_TOKEN"],
    "sonar": ["SONAR_TOKEN"],
    "pact": ["PACT_READONLY_PASSWORD"],
}


# ── Types ─────────────────────────────────────────────────────────────────────

CredentialState = Literal["lit", "partial", "dark"]


@dataclass(frozen=True)
class ServiceCredential:
    """Readiness report for a single service.

    Attributes:
        name: canonical service name (``"github"``, ``"atlassian"``, …)
        state: ``"lit"`` if every required env var is set,
               ``"dark"`` if none are set, ``"partial"`` otherwise.
        required: the full list of env var names this service needs.
        set_vars: the subset of ``required`` that are currently set
                  to a non-empty value.
        missing: the subset of ``required`` that are unset or empty.
    """

    name: str
    state: CredentialState
    required: list[str]
    set_vars: list[str]
    missing: list[str]


@dataclass(frozen=True)
class CredentialsReport:
    """Aggregate readiness report across all canonical services.

    Attributes:
        services: per-service readiness, ordered by ``CANONICAL_SERVICES``
                  iteration order (insertion order, which matches the
                  source dict — stable and predictable).
        lit: count of services in the ``"lit"`` state.
        partial: count of services in the ``"partial"`` state.
        dark: count of services in the ``"dark"`` state.
    """

    services: list[ServiceCredential]
    lit: int
    partial: int
    dark: int


# ── Check logic ───────────────────────────────────────────────────────────────


def _is_set(name: str, env: dict[str, str] | None = None) -> bool:
    """True if an env var is set to a non-empty value.

    Treats empty strings the same as unset — this matches common
    ``unset VAR`` semantics and guards against the "I cleared it with
    ``export VAR=``" case that would otherwise count as set.
    """
    source = env if env is not None else os.environ
    value = source.get(name, "")
    return value.strip() != ""


def check_service(
    name: str,
    required: list[str],
    env: dict[str, str] | None = None,
) -> ServiceCredential:
    """Check a single service's credential state.

    Args:
        name: canonical service name.
        required: list of env var names the service depends on.
        env: override the environment source for tests. Defaults to
             ``os.environ`` when ``None``.

    Returns:
        A ``ServiceCredential`` with lit/partial/dark state and the
        exact lists of set + missing var names. A service with zero
        required env vars is vacuously ``"lit"`` — nothing to check,
        nothing to miss.
    """
    # Vacuous case: a service with no requirements is trivially ready.
    # Catalog sanity tests prevent this firing against CANONICAL_SERVICES,
    # but callers passing their own service list deserve the consistent
    # behavior.
    if not required:
        return ServiceCredential(
            name=name,
            state="lit",
            required=[],
            set_vars=[],
            missing=[],
        )

    set_vars = [v for v in required if _is_set(v, env)]
    missing = [v for v in required if not _is_set(v, env)]

    if not set_vars:
        state: CredentialState = "dark"
    elif missing:
        state = "partial"
    else:
        state = "lit"

    return ServiceCredential(
        name=name,
        state=state,
        required=list(required),
        set_vars=set_vars,
        missing=missing,
    )


def check_credentials(
    services: dict[str, list[str]] | None = None,
    env: dict[str, str] | None = None,
) -> CredentialsReport:
    """Check readiness across a set of services.

    Args:
        services: override the service catalog for tests. Defaults to
                  ``CANONICAL_SERVICES``.
        env: override the environment source for tests. Defaults to
             ``os.environ``.

    Returns:
        A ``CredentialsReport`` aggregating per-service state.
    """
    source = services if services is not None else CANONICAL_SERVICES
    results = [check_service(name, required, env) for name, required in source.items()]

    lit = sum(1 for r in results if r.state == "lit")
    partial = sum(1 for r in results if r.state == "partial")
    dark = sum(1 for r in results if r.state == "dark")

    return CredentialsReport(
        services=results,
        lit=lit,
        partial=partial,
        dark=dark,
    )
