---
name: implement
description: >
  Execute a plan and make code changes. Invoke when the user says "let's build
  this", "start coding", "make the changes", "implement the plan", or "we've
  planned enough — time to write code".
argument-hint: "<project-name>"
---

# Implementation Mode

When this skill is invoked with a project name:

---

## 1. Locate workspace config

Read `assistant/config.json` (relative to the workspace root). Extract `projects_dir`.

If the config is missing, ask the user where project files are stored.

---

## 2. Load project context

Set `projectPath` to `{projects_dir}/{project-name}`.

Check for:
- `discovery.md` — set `hasDiscovery` flag if present
- `architecture.md` — set `hasArchitecture` flag if present
- `plan.md` — set `hasPlan` flag; warn if missing ("Consider running /plan first")

---

## 3. Status messages

```
📍 IMPLEMENTATION MODE for: {project-name}

Available context:
  ✓ discovery.md    (if present)
  ✓ architecture.md (if present)
  ✓ plan.md         (if present)
```

---

## 4. Mode behavior

You are now in IMPLEMENTATION MODE.

**Focus:**
- Execute the implementation plan
- Make code changes across identified repositories
- Write and update tests
- Handle edge cases discovered during implementation
- Update documentation as needed

**Approach:**
- Read all available context docs before starting
- Stay focused on the planned changes
- Communicate blockers or plan adjustments clearly
- Test changes as you go
- Ask for guidance when encountering unexpected issues

**Starting:**
- If a plan exists: review it and ask which part to begin with
- If no plan: ask what needs to be implemented

This is a collaborative partnership — communicate what you're doing and why.
