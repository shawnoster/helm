---
name: dev-discovery
description: Enter DISCOVERY MODE — collaboratively locate relevant code, repos, and context for a project
argument-hint: "<project-name>"
---

# Discovery Mode

When this skill is invoked with a project name:

---

## 1. Locate workspace config

Read `assistant/config.json` (relative to the workspace root). Extract `projects_dir`.

If the config is missing, ask the user where project files are stored before continuing.

---

## 2. Scaffold project files

Set `projectPath` to `{projects_dir}/{project-name}`.

Create the directory if it doesn't exist. Check for and create missing files:

- `README.md` — project hub with frontmatter (jira/ticket key, repos, confluence pages)
- `status.md` — current phase (set to "Discovery"), blockers, next actions
- `discovery.md` — affected repos, business context, requirements, entry points

If any file already exists, load it and resume from where it left off.

---

## 3. Status messages

New project:
```
✓ Created project structure at: {projectPath}
📍 DISCOVERY MODE ACTIVE
Track repositories in discovery.md's repos table.
```

Resuming:
```
📍 Resuming DISCOVERY MODE for: {project-name}
Loading: {discoveryPath}
```

---

## 4. Mode behavior

You are now in DISCOVERY MODE.

**Focus:**
- Locate relevant repositories — local and remote
- Track each repo in `discovery.md`'s Affected Repositories table (name, role, key modules, notes)
- Identify key files, entry points, APIs, and integration points
- Map dependencies between components
- Assess initial scope and complexity

**Boundaries:**
- Do NOT analyze implementation details (that's `/dev-architecture`)
- Do NOT create plans or solutions (that's `/dev-plan`)
- Do NOT make code changes (that's `/dev-implement`)
- Stay in discovery until the user explicitly switches modes

**Approach:**
- This is a COLLABORATIVE CONVERSATION — ask questions, share findings, wait for guidance
- Update `discovery.md` incrementally as insights emerge
- Do not work autonomously

If resuming: read `discovery.md`, summarize findings so far, then ask how to continue.
If new: ask about the context and what problem this project is solving.
