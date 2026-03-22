---
name: dev-architecture
description: Enter ARCHITECTURE MODE — analyze how an existing implementation works before planning changes
argument-hint: "<project-name>"
---

# Architecture Mode

When this skill is invoked with a project name:

---

## 1. Locate workspace config

Read `assistant/config.json` (relative to the workspace root). Extract `projects_dir`.

---

## 2. Load project context

Set `projectPath` to `{projects_dir}/{project-name}`.

Check for `discovery.md` — load it if present (required context for architecture analysis).

Check for `architecture.md` — create it if missing.

---

## 3. Status messages

New analysis:
```
✓ Initialized: architecture.md
📍 ARCHITECTURE MODE ACTIVE
```

Resuming:
```
📍 Resuming ARCHITECTURE MODE for: {project-name}
Loading: {architecturePath}
```

---

## 4. Mode behavior

You are now in ARCHITECTURE MODE.

**Focus:**
- Understand how the current implementation works
- Trace data flows, API contracts, and component interactions
- Identify the blast radius of proposed changes
- Document findings in `architecture.md`

**Approach:**
- Read repos and code identified in `discovery.md`
- Ask questions to fill gaps — don't assume
- Update `architecture.md` incrementally as understanding grows
- Do NOT make code changes (that's `/dev-implement`)
- Do NOT create implementation plans (that's `/dev-plan`)

If resuming: load `architecture.md`, summarize current understanding, then ask what to explore next.
If new: start from `discovery.md` and ask what aspect of the architecture to investigate first.
