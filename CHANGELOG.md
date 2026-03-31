# Changelog

## [Unreleased]

### Fixed

- Pin `coincurve<21` to avoid source build failure on Python 3.14 — coincurve 21.0.0 has a broken
  `hatch_build.py` that looks for cffi's LICENSE file during build, but cffi 2.0.0 changed sdist
  packaging so that file no longer exists in the expected location (closes #101). The pin will be
  lifted when coincurve ships cp314 wheels or cffi fixes its sdist packaging.

### Removed

- `aya bootstrap` and `aya reset` commands — workspace scaffolding is no longer part of aya's
  responsibilities. The guild workspace is the source of truth; aya is a tool the workspace calls.
- `scripts/bootstrap.py` — standalone workspace scaffolder script
- `templates/` directory — stale `AGENTS.md` and `CLAUDE.md` templates
- `framework/scripts/` directory — `scheduler.py`, `status_check.py`, `assistant_profile.py`,
  `watcher_daemon.py` (none were imported by the CLI; workspace content only)
- `skills/` directory — skill `SKILL.md` files belong in the user's guild workspace, not in aya

### Changed

- Renamed Python package from `ai-assist` to `aya`; CLI binary renamed from `assist` to `aya`
- Updated all internal imports, user-facing messages, docs, and tests accordingly
