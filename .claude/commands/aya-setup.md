---
name: aya-setup
description: >
  First-run bootstrap for a new aya installation. Invoke when the user says
  "set up aya", "initialize aya", "first time setup", "bootstrap aya",
  "new machine setup", or "install aya".
argument-hint: "<instance label, e.g. 'home' or 'work'>"
---

# Setup

Full first-run bootstrap: identity, hooks, relay polling, and optional pairing.

---

## Steps

1. **Check current state.** Run `aya status` to see what's already configured. If aya is fully set up (identity exists, hooks installed), report that and ask if the user wants to re-pair or reconfigure anything. Don't re-run setup unnecessarily.

2. **Initialize identity.** If no instance exists:

   ```bash
   aya init --label {label}
   ```

   Ask the user for a label if not provided (common choices: "home", "work", "laptop").

3. **Install hooks and crontab.**

   ```bash
   aya schedule install --dry-run
   ```

   Show the user what will be installed. If they approve:

   ```bash
   aya schedule install
   ```

4. **Set up relay polling.** Register a recurring relay poll so packets are received automatically:

   ```bash
   aya schedule recurring -m "relay-poll" -c "*/10 * * * *" \
     -p "Run: aya receive --instance {label} --auto-ingest --quiet. If any packets were ingested, surface their content to the user."
   ```

5. **Set up health break reminder** (optional — ask if the user wants it):

   ```bash
   aya schedule recurring -m "health-break" -c "*/20 * * * *" \
     -p "Deliver a health break reminder. Suggest standing up, stretching, getting water, and walking for at least 2 minutes. Keep it warm, brief, and varied — two sentences max." \
     --idle-back-off 10m
   ```

6. **Offer pairing.** Ask: "Do you want to pair with another machine now?" If yes, hand off to the `/pair` flow.

7. **Summary.** Report what was set up:

   ```
   aya setup complete:
   - Identity: {label} ({DID prefix}...)
   - Hooks: installed (SessionStart, PreToolUse, PostToolUse)
   - Crontab: */5 scheduler tick
   - Relay poll: every 10 minutes
   - Health break: every 20 minutes (with idle back-off)
   - Pairing: {paired with X | skipped}
   ```

---

## Notes

- This is idempotent — `aya schedule install` won't duplicate existing hooks.
- The system crontab runs `aya schedule tick --quiet` every 5 minutes for background polling.
- If the user already has an identity but no hooks, skip to step 3.
- If `aya` isn't installed at all, guide the user: `uv tool install aya-ai-assist`.
