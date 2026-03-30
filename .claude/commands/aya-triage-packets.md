---
name: aya-triage-packets
description: >
  Receive and process incoming aya packets. Invoke when the user says
  "check for packets", "triage packets", "what came in", "check the relay",
  "any messages", or "process incoming".
---

# Triage Packets

Receive packets from the relay, then interpret and route each one.

---

## Steps

0. **Resolve local identity.** Run `aya status` and read the identity name. Use that as `{identity}` in subsequent commands.

1. **Receive packets.** Run:

   ```bash
   aya receive --as {identity} --auto-ingest --quiet
   ```

   If nothing was received, report "No new packets" and stop.

2. **Check the inbox.** Run:

   ```bash
   aya inbox --as {identity}
   ```

   If the inbox is empty after receive, report "Inbox clear" and stop.

3. **Process each packet by type:**

   **Content packets** (`text/markdown`, `application/json`):
   - Summarize the content for the user.
   - Suggest where it belongs: daily note, inbox, a specific project file, or knowledge base.
   - Ask the user how to route it, or apply obvious routing if the intent is clear.

   **Seed packets** (`application/aya-seed`):
   - Surface the opener/question to the user.
   - Ask if they want to investigate now, defer it to inbox, or dismiss.
   - If they want to act on it, begin the research/task described in the seed.

4. **Report summary.** After processing:

   ```
   Processed {N} packets:
   - {summary of each packet and what was done with it}
   ```

---

## Notes

- Always use `--auto-ingest` to accept trusted packets without prompting.
- Use `--quiet` to suppress empty-inbox noise.
- If a packet's intent suggests urgency ("incident", "blocked", "need response"), flag it prominently.
- Seeds are questions, not answers — don't try to "file" them, act on them or defer them.
