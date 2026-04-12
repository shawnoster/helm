---
name: relay
description: >
  Manage communication between work and home instances via the aya relay.
  Covers checking inbox, reading packets, replying, sending new messages,
  and showing relay status. Invoke when the user says "check the relay",
  "any packets", "send to home", "ask work", "reply to that", "what did
  home say", "relay status", or any equivalent. Auto-polls after every
  send to catch in-flight replies.
argument-hint: "[check | read <id> | reply <id> | send <peer> <intent> | status]"
---

# Relay

Work ⇄ home communication via aya packets over a Nostr relay. This skill
wraps the four common verbs plus a status check, so the user gets a clean
back-and-forth without ever seeing raw packet JSON.

Always pass `--as <local-label>` (e.g. `--as home` on the home machine,
`--as work` on the work machine). The `default` identity is wrong on any
machine that has run `aya init` with a real label.

---

## 0. Route intent

| User says | Verb |
|---|---|
| "check the relay", "any packets", "check now" | 1. Check |
| "read that", "show packet", "what did home say" | 2. Read |
| "reply to that", "answer work" | 3. Reply |
| "send to home", "ask work about X" | 4. Send |
| "relay status", "is the relay up", "who's paired" | 5. Status |
| Ambiguous | Run verb 1 (Check), then ask |

---

## 1. Check

Poll **and** ingest in one shot. `--auto-ingest` ingests trusted packets
without prompting; `--skip-untrusted` prevents the command from blocking
on confirmation for unknown senders (non-interactive safety);
`--format json` forces structured output regardless of TTY detection
(default `auto` produces Rich text on a real terminal, which breaks
JSON parsing downstream).

```bash
aya receive --as <local-label> --auto-ingest --skip-untrusted --format json
```

For each new packet in the returned `packets` array, immediately run
verb 2 (Read) inline and present the body. Lead with the most recent.
Summarize multiple packets; don't dump the JSON list to the user.

If the response is `{"packets": []}`, reply *"Empty."* and stop.

**Signature failures** are handled by aya at the `receive` boundary:
the CLI logs `WARNING:aya.packet:DID-based signature verification
failed for packet <id>` to **stderr** and *discards* the packet from
the JSON output. Bad-sig packets do **not** appear with
`ingested: false` in the receive response. To surface them to the
user, capture stderr separately:

```bash
aya receive --as <local-label> --auto-ingest --skip-untrusted --format json 2>/tmp/aya-recv.err
grep -E "verification failed|InvalidSignature" /tmp/aya-recv.err
```

If a warning line appears, tell the user: *"packet `<id>` failed
signature verification and was discarded by aya — sender needs to
re-dispatch."* The packet itself stays on the relay and will resurface
on every poll until you explicitly drop it locally:

```bash
aya drop <packet-id> --as <local-label>
```

`aya drop` adds the ID to the local profile's `dropped_ids` list, and
both `aya inbox` and `aya inbox --all` filter it out from then on. The
drop is local to this profile — the packet stays on the relay until
natural expiry.

---

## 2. Read

Use `aya read <packet-id>` to extract the body cleanly without dumping
the envelope JSON. Add `--meta` to include id/from/sent_at/intent
header fields, and `--format json` for structured consumption.

```bash
aya read --meta --format json <packet-id>
```

The JSON output has shape:

```json
{
  "id": "01KP07VKNS...",
  "body": "<extracted text — opener+context+questions for seeds, content for markdown>",
  "from": "did:key:z6Mkqx…",
  "sent_at": "2026-04-12T07:00:21+00:00",
  "intent": "skill version check",
  "in_reply_to": null
}
```

Use those fields to populate a framing template in your response to
the user — never paste the raw JSON itself:

```
━━━ Packet <id_prefix> ━━━
From: <from>          Sent: <sent_at>
Intent: <intent>
<in_reply_to: <parent_id_prefix>, if present>

<body>
━━━━━━━━━━━━━━━━━━━━━━━━━━
```

For text-mode rendering directly to the user (no parsing needed):

```bash
aya read --meta <packet-id>
```

`aya read` returns DIDs in the `from` field, not human labels. To
resolve a DID to a label (e.g. `work` instead of `did:key:z6MkqxSg…`),
look it up via `aya inbox --as <local-label> --format json` (which
includes `from_label`) or via the local profile's `trusted_keys` map.
See verb 3 (Reply) for the lookup pattern.

For browsing past packets: `aya packets -n 10` (lists historical, not
just unread).

---

## 3. Reply

Always thread via `--in-reply-to`. The recipient comes from the original
packet — but `aya read` only returns the sender DID in `from`, not a
human label. Two options:

**Option A** (preferred when packet is still in inbox): resolve via
`aya inbox --format json`, which includes both `from_did` and
`from_label`:

```bash
PEER_LABEL=$(aya inbox --as <local-label> --format json | python3 -c "
import sys, json
data = json.loads(sys.stdin.read())
packets = data.get('packets', []) if isinstance(data, dict) else data
for p in packets:
    if p.get('id', '').startswith('<original-packet-id>'):
        print(p.get('from_label') or p.get('from_did', ''))
        break
")
```

**Option B** (fallback if packet has cleared the inbox): use the DID
from `aya read --meta` directly. `aya dispatch --to` accepts a DID as
well as a label.

```bash
PEER=$(aya read --meta --format json <original-packet-id> | python3 -c "
import sys, json
print(json.loads(sys.stdin.read())['from'])
")
```

Then dispatch:

```bash
aya dispatch --as <local-label> --to "$PEER_LABEL_OR_DID" \
  --intent "re: <condensed original intent>" \
  --seed \
  --in-reply-to <original-packet-id> \
  --opener "<reply body>"
```

For replies carrying long content or files, swap `--seed --opener` for
`--files <path>` or pipe markdown via stdin (see verb 4).

**Then immediately poll** (verb 1's command). The peer may have already
sent a follow-up while you were composing. Catching it now is free and
collapses round-trip latency. Surface anything new in the same response.

---

## 4. Send

Fresh dispatch, no thread. The user picks the peer; the skill picks the
type from the content shape.

### Type guide

| Use case | Form |
|---|---|
| Question or conversation starter | `--seed --opener "..."` (default) |
| Carrying notes, decisions, research | Pipe markdown via stdin |
| Sharing a file | `--files path/to/file.md` |
| Structured task handoff | Pipe markdown body |

### Seed (default — use unless content needs to ride along)

```bash
aya dispatch --as <local-label> --to <peer-label> \
  --intent "<one-line intent>" \
  --seed \
  --opener "<opening question or body>"
```

### Content (markdown body via stdin)

```bash
aya dispatch --as <local-label> --to <peer-label> \
  --intent "<one-line intent>" \
  --context "<why this is being sent>" <<'BODY'
<markdown content>
BODY
```

### File

```bash
aya dispatch --as <local-label> --to <peer-label> \
  --intent "<one-line intent>" \
  --files path/to/file.md
```

After every send: report packet ID (first 8 chars), relay, intent. **Then
immediately poll** per verb 1 — same reasoning as verb 3.

---

## 5. Status

Quick relay health check: identity, trusted peers, pending inbox count.
Honors `AYA_HOME` environment variable for non-default installs and
parses `aya inbox --format json` for the actual packet count.

```bash
python3 -c "
import json, os, pathlib, subprocess

# Honor AYA_HOME env var override (default: ~/.aya)
aya_home = pathlib.Path(os.environ.get('AYA_HOME') or '~/.aya').expanduser()
profile_path = aya_home / 'profile.json'

p = json.loads(profile_path.read_text())
aya = p.get('aya', {})
me = next(iter(aya.get('instances', {}).keys()), 'unknown')
trusted = [v.get('label', k[:16]) for k, v in aya.get('trusted_keys', {}).items()]
relays = aya.get('default_relays', [])

# Compute pending inbox count from the JSON CLI output
inbox_result = subprocess.run(
    ['aya', 'inbox', '--as', me, '--format', 'json'],
    capture_output=True, text=True
)
try:
    inbox_data = json.loads(inbox_result.stdout or '{}')
    packets = inbox_data.get('packets', []) if isinstance(inbox_data, dict) else inbox_data
    pending = len(packets)
except (json.JSONDecodeError, ValueError):
    pending = '?'

print(f'Instance:      {me}')
print(f'Trusted peers: {trusted}')
print(f'Pending inbox: {pending}')
print(f'Relays:        {relays}')
"
```

Present as:

```
━━━ Relay Status ━━━
Instance:       <label>
Trusted peers:  <peer labels>
Pending inbox:  <N> / empty
Relays:         <urls>
━━━━━━━━━━━━━━━━━━━━━
```

There is no `aya status --relay` subcommand — the python fallback above
reads the profile directly (respecting `AYA_HOME`) and shells out to
`aya inbox --format json` for the count. Workspace-level `aya status`
is a separate thing and doesn't cover relay state.

---

## Cross-cutting rules

1. **Always `--as <local-label>`.** The `default` identity is wrong on
   any machine that has run `aya init` with a real label. Both home and
   work have multi-instance profiles where `default` is a stub.

2. **Immediate poll after every send.** Built into verbs 3 and 4. Costs
   nothing, catches packets the peer sent while you were composing.
   Single biggest latency win for active exchanges.

3. **Never paste raw packet JSON to the user.** Always extract via
   `aya read` in verb 2 and present with the framing template. Raw
   JSON is for debugging only.

4. **Failed-signature packets are not silent.** If `aya receive` warns
   about verification failure, surface the packet ID + intent to the
   user. If the packet keeps re-surfacing on subsequent polls and the
   user has been informed, run `aya drop <id> --as <local-label>` to
   add it to the local profile's `dropped_ids` list. Drop is local-only
   — the packet stays on the relay until natural expiry, but
   `aya inbox` (and `--all`) filter it out from then on.

5. **Cross-instance attribution is unreliable.** If a peer claims "I
   already did X" in a packet body, verify via the relevant artifact
   (git log `--pretty=full` for `Co-Authored-By: Claude` trailers, file
   mtimes, etc.) before trusting it. Relay peers are amnesiac across
   sessions — same DID, same git identity, different memory. See
   `feedback_cross_instance_claims.md` in memory.

6. **Don't spin up `aya schedule recurring` as a polling cron.** It
   doesn't actually fire during active sessions — the scheduler defers
   to hooks that don't get pulled. Lean on immediate-poll-on-send
   (verbs 3 and 4) plus manual `check` (verb 1) instead. Only set up a
   cron if the user explicitly asks AND accepts the limitation.

7. **Never send secrets, credentials, or PII over the relay.** Packets
   are encrypted and signed but the network path is public Nostr relays.
   Treat packet content as durable, observable, and replayable.

---

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `aya receive` returns `{"packets": []}` but you expect one | Peer hasn't dispatched yet, or relay propagation lag | Tell user to ping the peer; wait 30s and retry |
| `WARNING:aya.packet:DID-based signature verification failed for packet <id>` on stderr | Bad signature; packet is **discarded** by aya, never appears in the JSON output (and not as `ingested:false`) | Surface to user; run `aya drop <id>` to stop the resurface; sender must re-dispatch to retry |
| `aya show <id>` returns `PACKET_NOT_FOUND` | Packet not yet ingested | Run verb 1 (Check) first |
| `aya dispatch` errors with `Unknown recipient '<label>'. Available: ...` | `--to <peer>` not in `trusted_keys` | Run `aya pair` to connect, or `aya trust <did> --peer <label>` |
| `aya dispatch` errors with `No Nostr pubkey found for recipient. Pair first.` | Trust entry exists but lacks `nostr_pubkey` field | Re-pair via `aya pair` to populate the pubkey |
| Interactive shell errors before aya runs | Shell function shadowing the binary | Check `declare -F aya`; unset if found |
| `aya schedule recurring` shows `last_run_at: never` | Hooks don't fire in active sessions | Expected; rely on manual check + immediate-poll |
| Relay returns HTTP 503 / connection refused | Transient relay outage | aya auto-retries (5 attempts); wait 30s and retry manually |

---

## Notes

- `/pack-for-home` is the shortcut for end-of-session handoffs from work
  to home. This skill handles everything else, including the reverse
  direction and mid-session exchanges.
- Seed packets are lighter and safer for questions; content packets carry
  material. Default to seeds.
- The relay is asymmetric in practice: home runs hooks/cron-backed
  pulling; work side is human-triggered (Shawn says "check" and the
  work-side instance polls). Don't assume both ends have the same cadence.
