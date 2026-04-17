---
name: relay
description: >
  Manage communication between instances and peers via the aya relay.
  Covers checking inbox, reading packets, replying, sending new messages,
  and showing relay status. Invoke when the user says "check the relay",
  "any packets", "send to home", "send this to work", "tell Sean",
  "pack this up", "ask work", "reply to that", "what did home say",
  "relay status", "anything new?", or any equivalent. Infers intent
  and recipient from context. Auto-polls after every send.
argument-hint: "[check | read <id> | reply <id> | send [<peer>] [<intent>] | status]"
---

# Relay

Work ⇄ home communication via aya packets over a Nostr relay. This skill
wraps the four common verbs plus a status check, so the user gets a clean
back-and-forth without ever seeing raw packet JSON.

Always pass `--as <local-label>` (e.g. `--as home` on the home machine,
`--as work` on the work machine). The `default` identity is wrong on any
machine that has run `aya init` with a real label.

---

## Tool surface

When the aya MCP server is connected, prefer the `aya_*` tools listed
inline with each verb below. Fall back to the CLI when any of these apply:

- The MCP server isn't connected (no `aya_*` tools in the available set).
- The operation needs a flag the MCP tool doesn't expose — notably
  `--seed --opener` for lightweight seed packets, `--files` for file
  attachments, or stderr capture for signature-verification warnings.
- The operation has no MCP equivalent: `aya drop`, `aya pair`,
  `aya init`, `aya schedule install`, `aya schedule recurring`,
  `aya schedule dismiss`.

MCP tools that act on a local identity (`aya_receive`, `aya_inbox`,
`aya_send`, `aya_ack`, `aya_relay_status`) take `instance=<label>`
where the CLI takes `--as <label>`. Other MCP tools (`aya_read`,
`aya_show`, `aya_packets`, `aya_status`, `aya_schedule_*`,
`aya_config_*`) don't take an identity argument — they act on local
state or a specific packet ID. `aya_receive` auto-ingests trusted
packets by default, so no `--auto-ingest`/`--skip-untrusted`
equivalents are needed.

---

## 0. Route intent

### Explicit subcommands (highest priority)

`/relay check`, `/relay send work`, `/relay reply <id>`, `/relay status`
→ use the verb directly, no inference needed.

### Keyword routing

| User says | Verb |
|---|---|
| "check the relay", "any packets", "check now", "anything new?" | 1. Check |
| "read that", "show packet", "what did home say" | 2. Read |
| "reply to that", "answer work", "respond to Sean's packet" | 3. Reply |
| "send to home", "ask work about X", "tell Sean about the design" | 4. Send |
| "relay status", "is the relay up", "who's paired" | 5. Status |

### Context inference (when keywords don't match)

| User says | Inferred verb + context |
|---|---|
| "pack this up for work" | Send (recipient: work, curation mode) |
| "I'm done for the day, send this home" | Send (recipient: home, curation mode) |
| "anything new from Sean?" | Check (filter results by sender) |
| "what did work say about the design?" | Check → Read (search by intent/content) |
| Just `/relay` with no context | Status, then ask "What do you need?" |

### Recipient inference (for send/reply)

1. Infer from phrasing — "send to work" → `work`, "tell Sean" → `sean-okeefe`
2. If ambiguous, show a picker from trusted keys:
   ```
   Who should I send this to?
     1. work
     2. sean-okeefe
   ```
3. Validate with `printf 'validate' | aya send --dry-run --as <local-label> --to <label> --intent validate`

---

## 1. Check

Poll **and** ingest in one shot.

**MCP (preferred):** `aya_receive(instance="<local-label>")` — auto-ingests
trusted packets and skips untrusted ones by default; no flags needed.

**CLI fallback** — use when MCP isn't available, or when capturing
stderr to surface signature-verification warnings (see below):

```bash
aya receive --as <local-label> --auto-ingest --skip-untrusted --format json
```

CLI flag notes: `--auto-ingest` ingests trusted packets without
prompting; `--skip-untrusted` prevents blocking on confirmation for
unknown senders (non-interactive safety); `--format json` forces
structured output regardless of TTY detection (default `auto` produces
Rich text on a real terminal, which breaks JSON parsing downstream).

The two surfaces return different shapes:

- **MCP `aya_receive`** returns a list of packet summaries directly,
  e.g. `[{id, intent, from, ingested}, …]`. Empty list means nothing
  new.
- **CLI `aya receive --format json`** wraps the list in an object:
  `{"packets": [{...}, …]}`. Empty is `{"packets": []}`.

For each new packet, immediately run verb 2 (Read) inline and present
the body. Lead with the most recent. Summarize multiple packets; don't
dump the JSON list to the user.

If the returned list is empty (`[]` from MCP, `{"packets": []}` from
CLI), reply *"Empty."* and stop.

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
re-send."* The packet itself stays on the relay and will resurface
on every poll until you explicitly drop it locally:

```bash
aya drop <packet-id> --as <local-label>
```

`aya drop` is CLI-only (no MCP equivalent). It adds the ID to the local
profile's `dropped_ids` list, and both `aya inbox` and `aya inbox --all`
filter it out from then on. The drop is local to this profile — the
packet stays on the relay until natural expiry.

---

## 2. Read

Extract the body cleanly without dumping the envelope JSON. Include
`meta`/`--meta` to get id/from/sent_at/intent header fields alongside
the body.

**MCP (preferred):** `aya_read(packet_id="<id>", meta=true)`.

**CLI fallback:**

```bash
aya read --meta --format json <packet-id>
```

The two surfaces return different shapes — use the right key for the
surface you called:

- **MCP `aya_read(meta=true)`** returns
  `{id, intent, from, sent_at, content_type, content}`. The `content`
  field is the raw packet content (no extraction). No `in_reply_to`
  field — use `aya_show(packet_id=...)` if you need the full envelope.
- **CLI `aya read --meta --format json`** returns
  `{id, body, from, sent_at, intent, in_reply_to}`. The `body` field
  is already *extracted* text (opener+context+questions for seeds,
  content for markdown).

Populate the framing template below with the common fields (`from`,
`sent_at`, `intent`) — never paste the raw JSON itself. For the body
line, use MCP's `content` or CLI's `body`. `in_reply_to` is CLI-only in
the template; omit it when the source is MCP.

```
━━━ Packet <id_prefix> ━━━
From: <from>          Sent: <sent_at>
Intent: <intent>
<in_reply_to: <parent_id_prefix>, if present — CLI source only>

<content or body>
━━━━━━━━━━━━━━━━━━━━━━━━━━
```

For text-mode rendering directly to the user (no JSON parse), CLI is
simpler:

```bash
aya read --meta <packet-id>
```

Both surfaces return DIDs in the `from` field, not human labels. To
resolve a DID to a label (e.g. `work` instead of `did:key:z6MkqxSg…`),
look it up via `aya_inbox(instance="<local-label>")` (MCP, preferred)
or `aya inbox --as <local-label> --format json` (CLI) — both include
`from_label`. Or read the local profile's `trusted_keys` map directly.
See verb 3 (Reply) for the lookup pattern.

For browsing past packets: `aya_packets(limit=10)` (MCP) or
`aya packets -n 10` (CLI).

---

## 3. Reply

Always thread via `in_reply_to` / `--in-reply-to`. The recipient comes
from the original packet — but the `from` field is a sender DID, not a
human label. Two lookup options:

**Option A** (preferred when packet is still in inbox): resolve the
label via inbox. The MCP tool returns structured data directly:

- **MCP:** `aya_inbox(instance="<local-label>")` → find the entry
  whose `id` starts with `<original-packet-id>`, read `from_label`.
- **CLI:**

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
from `aya_read(packet_id, meta=true)` (MCP) or
`aya read --meta --format json <id>` (CLI). The send surfaces accept a
DID as well as a label.

Then send the reply. Choose the form that fits the content:

- **Content reply (markdown body, MCP preferred):**

  `aya_send(to="<peer_label_or_did>", intent="re: <condensed intent>",
  content="<markdown reply>", instance="<local-label>",
  in_reply_to="<original-packet-id>")`

- **Seed reply (short opener, CLI-only — `aya_send` has no seed mode):**

  ```bash
  aya send --as <local-label> --to "$PEER_LABEL_OR_DID" \
    --intent "re: <condensed original intent>" \
    --seed \
    --in-reply-to <original-packet-id> \
    --opener "<reply body>"
  ```

For replies carrying files, use CLI `--files <path>` (no MCP equivalent).

**Then immediately poll** (verb 1's command). The peer may have already
sent a follow-up while you were composing. Catching it now is free and
collapses round-trip latency. Surface anything new in the same response.

---

## 4. Send

Fresh send, no thread. Recipient is inferred or picked (see §0).
Content is either provided explicitly or curated from the conversation.

### Step 1 — Determine content source

**Explicit content (skip curation):**
- `/relay send work --files design.md` → send the file
- "send Sean this: <quoted text>" → send the quoted text
- Content piped via heredoc → send as-is

**No explicit content (curation mode):**
When the user says "pack this up" or "send this to work" without
specifying what "this" is, review the current conversation and assemble
a packet.

### Step 2 — Curate (when no explicit content)

Review the conversation for content worth sending. Prioritize:

- **Open decisions** — questions still unresolved, choices being weighed
- **Action items** — things flagged for follow-up
- **Context switches** — project state that would be lost without handoff
- **In-progress notes** — working docs, drafts, research

Filter out:
- Noise (linter output, large diffs, routine tool calls)
- Content irrelevant to the recipient (work-only tickets when sending
  home, personal notes when sending to a coworker)
- Sensitive credentials

Choose packet type:
- **Content** (markdown via stdin) — structured markdown, 100-500 words.
  Use when there's substantive material to carry
- **Seed** (`--seed --opener`) — opener question + 2-3 sentence context.
  Use for lightweight "start a conversation about X" or when there's no
  document-like content. Default when curation produces a short result.

Derive the intent from the content if the user didn't provide one: one
short sentence, first person, e.g. "Pick up dinner party guest count
decision" or "Continue reading list research".

**Show draft before sending:** "Here's what I'd send — look right?"

### Step 3 — Send

### Type guide

| Use case | Form |
|---|---|
| Question or conversation starter | `--seed --opener "..."` (default) |
| Carrying notes, decisions, research | Pipe markdown via stdin |
| Sharing a file | `--files path/to/file.md` |
| Structured task handoff | Pipe markdown body |

### Seed (default — use unless content needs to ride along)

Seed mode is **CLI-only** — `aya_send` has no `--seed --opener` equivalent:

```bash
aya send --as <local-label> --to <peer-label> \
  --intent "<one-line intent>" \
  --seed \
  --opener "<opening question or body>"
```

### Content (markdown body)

- **MCP (preferred):** `aya_send(to="<peer-label>",
  intent="<one-line intent>", content="<markdown content>",
  instance="<local-label>")`.

- **CLI fallback** (also the path when you want to thread via
  `--context`, which MCP doesn't expose):

  ```bash
  aya send --as <local-label> --to <peer-label> \
    --intent "<one-line intent>" \
    --context "<why this is being sent>" <<'BODY'
  <markdown content>
  BODY
  ```

### File

File attachments are **CLI-only** (no `--files` in `aya_send`):

```bash
aya send --as <local-label> --to <peer-label> \
  --intent "<one-line intent>" \
  --files path/to/file.md
```

After every send: report packet ID (first 8 chars), relay, intent. **Then
immediately poll** per verb 1 — same reasoning as verb 3.

---

## 5. Status

Quick relay health check: identity, trusted peers, pending inbox count.

**MCP (preferred):** call both `aya_relay_status(instance="<local-label>")`
(returns identity, trusted peers, relay URLs) and
`aya_inbox(instance="<local-label>")` (returns pending packet list — count
its length). Combine the two into the display template below.

**CLI fallback** — use when MCP isn't connected or when `AYA_HOME` is
set to a non-default location. This reads `profile.json` directly and
shells out to `aya inbox` for the count:

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

There is no `aya status --relay` subcommand — the CLI fallback above
reads the profile directly (respecting `AYA_HOME`) and shells out to
`aya inbox --format json` for the count. Workspace-level `aya status`
(and its MCP tool `aya_status`) is a separate thing and doesn't cover
relay state.

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
| `aya receive` returns `{"packets": []}` but you expect one | Peer hasn't sent yet, or relay propagation lag | Tell user to ping the peer; wait 30s and retry |
| `WARNING:aya.packet:DID-based signature verification failed for packet <id>` on stderr | Bad signature; packet is **discarded** by aya, never appears in the JSON output (and not as `ingested:false`) | Surface to user; run `aya drop <id>` to stop the resurface; sender must re-send to retry |
| `aya show <id>` returns `PACKET_NOT_FOUND` | Packet not yet ingested | Run verb 1 (Check) first |
| `aya send` errors with `Unknown recipient '<label>'. Available: ...` | `--to <peer>` not in `trusted_keys` | Run `aya pair` to connect, or `aya trust <did> --peer <label>` |
| `aya send` errors with `No Nostr pubkey found for recipient. Pair first.` | Trust entry exists but lacks `nostr_pubkey` field | Re-pair via `aya pair` to populate the pubkey |
| Interactive shell errors before aya runs | Shell function shadowing the binary | Check `declare -F aya`; unset if found |
| `aya schedule recurring` shows `last_run_at: never` | Hooks don't fire in active sessions | Expected; rely on manual check + immediate-poll |
| Relay returns HTTP 503 / connection refused | Transient relay outage | aya auto-retries (5 attempts); wait 30s and retry manually |

---

## Notes

- End-of-session handoffs ("pack this up for work/home") are handled by
  verb 4 (Send) with content curation. No separate handoff skill is
  needed.
- Seed packets are lighter and safer for questions; content packets carry
  material. Default to seeds.
- The relay is asymmetric in practice: home runs hooks/cron-backed
  pulling; work side is human-triggered (Shawn says "check" and the
  work-side instance polls). Don't assume both ends have the same cadence.
