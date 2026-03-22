---
name: finish
description: >
  Close out a completed piece of work — commit, push, open a PR, transition
  the ticket to In Review, and log the activity. Invoke when the user says
  "I'm done", "open a PR", "ship it", "wrap this up", "commit and push", or
  after implementation work is complete and ready for review.
argument-hint: "[<project-name>]"
---

# Finish

Close the implementation loop. Commit, push, PR, ticket, log — then hand off.

---

## 1. Check working state

Run `git status`. Determine:

- **Uncommitted changes**: list changed files
- **Current branch**: confirm it's a feature branch (not `main` or the repo default)
- **Already pushed**: check whether the branch has a remote tracking branch
- **Existing PR**: check whether a PR already exists for this branch

If the working tree is already clean and a PR already exists, skip to step 4 (Update the ticket).

---

## 2. Commit

If there are uncommitted changes:

- Show a summary of changed files
- Suggest a commit message following [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `chore:`, `docs:`, etc.)
- Wait for confirmation before committing — do not commit without approval

After committing, confirm the commit hash and message.

---

## 3. Push and open the PR

Push the branch to the remote.

If a PR already exists: show the URL and current state (CI, review status). Skip creation.

If no PR exists, create one:
- **Title**: derived from the ticket summary or branch name, Conventional Commits format
- **Body**: use the repo's PR template if present; otherwise: Summary, Type of change, Related issues, Test plan, Checklist
- **Base branch**: `main` (or the repo's default)
- **Draft?**: ask — "Ready for review, or start as draft?"

Output the PR URL.

---

## 4. Update the ticket

If a project tracking integration is connected and a ticket was associated with this branch:

- Transition the ticket to **In Review** (or the equivalent status in this project's workflow)
- Link the PR to the ticket if the integration supports it

If the ticket is already In Review or further along, note it and skip.

---

## 5. Log the activity

Append to `assistant/notes/daily/{TODAY}.md` under `## Activity Log`:

```
[{HH:MM}] Opened PR #{number} — {PR title} · {ticket ref}
```

If no daily file exists for today, create it with the standard daily note frontmatter followed by the Activity Log section:

```markdown
---
date: {TODAY}
---

## Activity Log
```

---

## 6. Hand off

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Shipped.

  Branch:  {branch-name}
  PR:      #{number} — {title}  ({draft / ready for review})
  Ticket:  {key} → In Review

  What's next?
  · Keep going on this project
  · Check signals and get a next suggestion  (/pivot)
  · Call it a day  (/eod)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Wait for the user's choice. If they say "pivot" or "what's next", run `/pivot`. If "done" or "eod", run `/eod`.

---

## Notes

- Never force-push or amend published commits
- If the working tree has unrelated changes (wrong files staged), flag them and ask before proceeding
- If CI is not yet passing, note it in the PR description and suggest starting as draft
- The PR checklist items come from the repo's PR template — don't skip them
- If there is no ticket (no integration, or branch was created without one), skip step 4 gracefully
