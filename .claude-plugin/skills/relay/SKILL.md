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

Poll **and** ingest in one shot. `--auto-ingest` does both — no separate
`aya inbox` call.

```bash
aya receive --as <local-label> --auto-ingest --quiet
```

For each new packet returned, immediately run verb 2 (Read) inline and
present the body. Lead with the most recent. Summarize multiple packets;
don't dump the JSON list to the user.

If the response is `{"packets": []}`, reply *"Empty."* and stop.

If `aya receive` warns about a verification failure (e.g.
`InvalidSignature`), surface it explicitly: *"packet `<id>` failed
verification — not auto-ingested. Inspect with care or ignore."* Do not
silently re-poll past it.

---

## 2. Read

Show the body of a previously ingested packet without dumping the
envelope.

```bash
aya show <packet-id> 2>&1 | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
c = d.get('content')
if isinstance(c, dict):
    print(c.get('opener', ''))
    if c.get('context_summary'):
        print('\\n--- context ---')
        print(c['context_summary'])
    if c.get('open_questions'):
        print('\\n--- open questions ---')
        for q in c['open_questions']:
            print(f'- {q}')
elif isinstance(c, str):
    print(c)
"
```

Then present to the user with framing:

```
━━━ Packet <id_prefix> ━━━
From: <from_label>   Sent: <sent_at>
Intent: <intent>
<in_reply_to: <parent_id_prefix>, if present>

<extracted body>
━━━━━━━━━━━━━━━━━━━━━━━━━━
```

For browsing past packets: `aya packets -n 10` (lists historical, not
just unread).

---

## 3. Reply

Always thread via `--in-reply-to`. Pull the peer label from the original
packet's `from_label` field — don't ask the user, they already pointed
at the packet.

```bash
aya dispatch --as <local-label> --to <peer-label> \
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

```bash
aya inbox --as <local-label> 2>&1
python3 -c "
import json, pathlib
p = json.loads(pathlib.Path('~/.aya/profile.json').expanduser().read_text())
aya = p.get('aya', {})
me = next(iter(aya.get('instances', {}).keys()), 'unknown')
trusted = [v.get('label', k[:16]) for k, v in aya.get('trusted_keys', {}).items()]
print(f'This instance: {me}')
print(f'Trusted peers: {trusted}')
print(f'Default relays: {aya.get(\"default_relays\", [])}')
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
reads the profile directly. Workspace-level `aya status` is a separate
thing and doesn't cover relay state.

---

## Cross-cutting rules

1. **Always `--as <local-label>`.** The `default` identity is wrong on
   any machine that has run `aya init` with a real label. Both home and
   work have multi-instance profiles where `default` is a stub.

2. **Immediate poll after every send.** Built into verbs 3 and 4. Costs
   nothing, catches packets the peer sent while you were composing.
   Single biggest latency win for active exchanges.

3. **Never paste raw packet JSON to the user.** Always extract via the
   python one-liner in verb 2 and present with the framing template.
   Raw JSON is for debugging only.

4. **Failed-signature packets are not silent.** If `aya receive` warns
   about verification failure, surface the packet ID + intent to the
   user. The bad-sig packet stays in `aya inbox` re-surfacing every poll
   until aya grows an explicit ack/drop command — that's a known gap,
   not your fault, but the user needs to know it's stuck.

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
| `aya receive` returns nothing but you expect a packet | Peer hasn't checked their relay yet | Tell user to ping the peer to check |
| Packet listed but `ingested: false` | Signature verification failed | Flag to user; do not auto-ingest |
| `aya show <id>` returns `PACKET_NOT_FOUND` | Packet not yet ingested | Run verb 1 (Check) first |
| `aya dispatch` errors with "no trusted key" | `--to <peer>` label not in profile | Run `aya pair` to connect, or `aya trust <did>` |
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
