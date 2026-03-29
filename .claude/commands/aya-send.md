---
name: aya-send
description: >
  Pack and dispatch a packet to another machine. Invoke when the user says
  "send this to home", "send this to work", "pack for home", "dispatch to work",
  "send context", or wants to share files/notes with another instance.
argument-hint: "<recipient> [message or intent]"
---

# Send

Pack and dispatch a packet to another aya instance in one guided step.

---

## Steps

1. **Resolve the recipient.** If the user provided a target (e.g., "home", "work"), use it as the `--to` value. If not, run `aya status` to list known peers and ask which one.

2. **Determine packet type.** Ask or infer:
   - **Content packet** (default): sharing knowledge, notes, files, context.
   - **Seed packet** (`--seed`): sending a question or research request for the other machine to act on. If seed, also determine an `--opener` (the opening question).

3. **Determine intent.** If the user said why they're sending (e.g., "context sync", "research request"), use that. Otherwise, summarize the purpose in a short phrase.

4. **Gather content.** Identify what to send:
   - If the user named specific files, use `--files`.
   - If the user described content inline, write it to a temp file and include it.
   - If the user said "send this conversation" or similar, summarize the relevant context into a temp markdown file.

5. **Determine the local instance.** Use `--instance home` unless the user specifies otherwise or context suggests a different instance.

6. **Confirm before sending.** Show the user what will be dispatched:

   ```
   Sending to: {recipient}
   Type: {content | seed}
   Intent: {intent}
   Files: {file list or "inline content"}
   ```

   Wait for confirmation.

7. **Dispatch.** Run the appropriate command:

   ```bash
   # Content packet
   aya dispatch --instance {instance} --to {recipient} \
     --intent "{intent}" --files {files}

   # Seed packet
   aya dispatch --instance {instance} --to {recipient} --seed \
     --intent "{intent}" --opener "{question}"
   ```

8. **Report result.** Confirm success or surface any errors.

---

## Notes

- If the user says "pack for home" or "send this home", that's a content packet to `home`.
- If the user says "ask work to investigate X", that's a seed packet to `work`.
- Keep intents short and descriptive — they're metadata, not the content.
- Packets expire after 7 days by default.
- The recipient must be a trusted/paired instance. If dispatch fails with an unknown recipient, suggest running `aya pair`.
