---
name: aya-pair
description: >
  Guided pairing between two aya instances. Invoke when the user says
  "pair with work", "pair instances", "set up pairing", "trust another machine",
  or "connect my machines".
argument-hint: "<remote label, e.g. 'work' or 'laptop'>"
---

# Pair

Walk through pairing this aya instance with another machine.

---

## Steps

1. **Check identity exists.** Run `aya status`. If no instance is initialized, run `aya init --label {label}` first (ask the user what label to use — typically "home", "work", or "laptop").

2. **Determine role.** Ask the user:
   - **"Are you starting the pairing, or do you have a code from the other machine?"**
   - If starting (initiator): go to step 3.
   - If joining with a code: go to step 4.

3. **Initiator flow.**

   ```bash
   aya pair --peer {remote_label} --as {local_instance}
   ```

   This prints a pairing code (e.g., `ANCHOR-NORTH-0045`). Tell the user:

   > **Pairing code: `{CODE}`**
   > Enter this code on your other machine within 10 minutes.
   > Run: `aya pair --code {CODE} --peer {this_machine_label} --as {other_instance}`

   Then wait. The command will poll the relay for the response and complete automatically.

4. **Joiner flow.** The user has a code from the other machine.

   ```bash
   aya pair --code {CODE} --peer {remote_label} --as {local_instance}
   ```

   - `--code`: the pairing code from the other machine.
   - `--peer`: what to call the remote peer (e.g., "home" if pairing from work).
   - `--as`: this machine's local identity.

5. **Verify.** After pairing completes, run `aya status` to confirm the trusted key was added. Report success:

   ```
   Paired successfully.
   Trusted: {remote_label} ({remote DID prefix}...)
   ```

---

## Notes

- Pairing codes expire after 10 minutes.
- Both machines must have `aya init` completed before pairing.
- The `--peer` flag names the *remote* peer, not the local machine.
- If pairing fails with a timeout, suggest trying again — relay propagation can be slow.
- After pairing, suggest running `aya schedule install` if hooks aren't set up yet.
