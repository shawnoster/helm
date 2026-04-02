---
name: aya-status
description: >
  Show aya system status — identity, alerts, watches, scheduler, and focus.
  Invoke when the user says "aya status", "check aya", "what's pending", "any alerts",
  "what's watching", or "how's the relay".
---

# aya status

Run a full aya readiness check and surface anything actionable.

## Steps

1. **Run status:**

   ```bash
   aya status -f json
   ```

2. **Parse and present.** Render in this order, skipping empty sections:

   ### Systems
   - Show a single line: `✓ systems ok` or list any failing checks with their detail.

   ### Alerts (`alerts[]`)
   - List each alert as `• {id[:8]} — {message}`.
   - If there are more than 5, show the first 5 and note how many more.
   - If none: skip this section.

   ### Watches (`watches[]`)
   - List each active watch: `• {id[:8]} — {target} ({provider})`.
   - If none: skip this section.

   ### Due reminders (`due[]`)
   - List each: `• {message}` — these need attention now.
   - If none: skip this section.

   ### Upcoming (`upcoming[]`)
   - List the next 3: `• {message} — {due}`.
   - If none: skip this section.

   ### Perspective
   - Print the `perspective` field as a closing line (Ship Mind voice, no label).

3. **Offer next steps** if there are alerts:
   - "Want me to triage these alerts?" → run `/aya-triage-packets`
   - "Want to dismiss any?" → `aya schedule dismiss {id}`
