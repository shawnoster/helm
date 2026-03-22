---
name: feature
description: >
  Start a new feature — find or create a ticket, create a branch, and set
  it In Progress. Invoke when the user says "start a feature", "let's work
  on X", "create a branch for X", "pick up ticket CSD-N", or "new feature
  for X".
argument-hint: "[<ticket-id> | <short description>]"
---

# Start Feature

Bootstrap a new feature from ticket to branch. Pre-flight first, then scaffold.

---

## 1. Pre-flight

Before anything else, confirm:

1. **Integrations** — check that code hosting (GitHub/GitLab) and project tracking (Jira/Linear/etc.) are accessible; surface any that are down before continuing
2. **Git status** — working tree must be clean; if not, surface the diff and ask before proceeding
3. **Base branch** — confirm current branch is `main` (or the repo's default branch) and up to date

---

## 2. Resolve the ticket

Parse the argument if provided.

### If a ticket ID is given (e.g. `CSD-225`, `ENG-42`)

- Fetch the ticket from the connected project tracking integration
- Display: key, summary, status, assignee
- Confirm this is the right ticket before continuing

### If a description is given (or no argument)

- Search the project tracking integration for existing tickets matching the description
- Show top matches and ask if any match, or if a new ticket should be created
- If no project tracking integration is available, skip this step and proceed with a branch name derived from the description

### If creating a new ticket

- Ask for project key, issue type, and summary
- Create the ticket; display the new key

---

## 3. Create the branch

Branch name format: `<type>/<ticket-id>-<short-slug>`

- Derive the slug from the ticket summary: lowercase, hyphens, max 5 words
- Default type: `feat` — adjust to `fix`, `chore`, `spike` based on issue type
- If no ticket ID, use just `<type>/<short-slug>`

```bash
git checkout main  # or default branch
git pull
git checkout -b <branch-name>
```

Confirm the branch was created and show the full name.

---

## 4. Set ticket In Progress

If a project tracking integration is available, transition the ticket to **In Progress**.

---

## 5. Confirm and hand off

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Feature ready

  Ticket:   {KEY} — {summary}
  Branch:   {branch-name}
  Status:   In Progress

  Next: implement, then open a PR when ready.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Notes

- Always branch from `main` (or the repo's default branch) unless the user specifies otherwise
- Never skip the pre-flight
- If the ticket is already In Progress or assigned to someone else, flag it and ask before proceeding
