# Changelog

## [Unreleased]

### Removed

- `aya pack` — `aya send` is the canonical pack-and-publish flow. The pack
  command had no callers in skills, hooks, or MCP — its help even redirected
  users to `send`. If you need to build a packet without publishing, use
  `aya send --dry-run` or build the JSON manually and use `aya send-raw`.
- `aya show` — collapsed into `aya read`. Pass `--panel` to `read` for the
  boxed display the old `show` produced. The MCP `aya_show` tool is removed
  with the same migration: `aya_read(meta=true)` returns the structured
  fields, and reading the packet file at `~/.aya/packets/<id>.json` gives
  the full signed envelope.
- `aya schedule check` — partial reimplementation of `pending` and `alerts`.
  Use `aya schedule pending` (the SessionStart payload) for due reminders
  + actionable alerts, or `aya schedule alerts [--mark-seen]` for the
  alert queue alone.
- `aya schedule poll` — replaced by `aya schedule tick`, which already calls
  `run_poll` internally. The command had been documented as legacy since the
  unified tick refactor; nothing in skills, hooks, or the system crontab
  calls it.
- `aya profile` — the persistent assistant profile is created and touched by
  `aya init` and `aya pair`; the inspect-only verb had no callers in skills,
  hooks, or the MCP surface. To inspect a profile, read `~/.aya/profile.json`
  or use `aya status`.
- Hidden deprecated flags `--label` (alias for `--peer` on `trust` and `pair`)
  and `--instance` (alias for `--as` on `pack`, `send`, `send-raw`, `ack`,
  `receive`, `inbox`, `pair`). They had been emitting warnings since the
  rename in #230; switch any remaining call sites to the canonical flag names.

### Fixed

- `aya receive` no longer drops pending packets via a stale `since` cursor
  (#247). The previous behaviour persisted a per-instance "last checked"
  timestamp and used it as the relay query lower bound, which permanently
  excluded packets that had arrived before the cursor but hadn't been
  ingested yet (e.g. when a prior receive crashed). Deduplication now
  uses the local `ingested_ids` list/dedup cache against the relay's natural 7-day
  TTL window, so unfinished receives can recover on the next run.
- Pin `coincurve<21` to avoid source build failure on Python 3.14 — coincurve 21.0.0 has a broken
  `hatch_build.py` that looks for cffi's LICENSE file during build, but cffi 2.0.0 changed sdist
  packaging so that file no longer exists in the expected location (closes #101). The pin will be
  lifted when coincurve ships cp314 wheels or cffi fixes its sdist packaging.

### Changed

- Refactor: packet ingestion logic lifted out of `cli.py` into a shared
  `aya.ingest` module (#245). Both the CLI `aya receive` command and the
  MCP `aya_receive` tool now share the same code path. User-facing
  behaviour is unchanged.

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
