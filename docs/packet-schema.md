# Packet Envelope Schema

**Protocol version:** `aya/0.2`

A packet is the unit of transfer in aya's inter-instance protocol.
Each packet is a signed JSON envelope that carries content between assistant instances, routed through Nostr relays with end-to-end encryption.
This document specifies the envelope structure, content types, conflict resolution strategies, and signing mechanics.

---

## Envelope Fields

| Field | Type | Required | Default | Description |
| ----- | ---- | -------- | ------- | ----------- |
| `id` | string (ULID) | auto | Generated at creation | Unique identifier for the packet. |
| `version` | string | auto | `"aya/0.2"` | Protocol version string. |
| `from` | string (did:key) | **required** | — | Sender's `did:key` URI. JSON alias for internal field `from_did`. |
| `to` | string (did:key) | **required** | — | Recipient's `did:key` URI. JSON alias for internal field `to_did`. |
| `sent_at` | string (ISO 8601) | auto | Current UTC time | Timestamp when the packet was created. |
| `expires_at` | string (ISO 8601) | auto | `sent_at` + 7 days | Expiration timestamp. See [Expiry](#expiry). |
| `intent` | string | **required** | — | Freeform string describing the packet's purpose (e.g. `"daily-sync"`, `"project-handoff"`). |
| `context` | string \| null | optional | `null` | Freeform context string providing additional background for the receiver. |
| `content_type` | string (enum) | optional | `"text/markdown"` | MIME-like type of the `content` field. See [Content Types](#content-types). |
| `content` | string \| object | optional | `""` | Payload. Structure depends on `content_type`. |
| `reply_to` | string \| null | optional | `null` | Legacy field, unused. |
| `in_reply_to` | string \| null | optional | `null` | Packet ID this packet is a reply to. Used by ACK packets. |
| `conflict_strategy` | string (enum) | optional | `"last_write_wins"` | How the receiver should handle conflicts. See [Conflict Strategies](#conflict-strategies). |
| `tags` | list\[string\] | optional | `[]` | Arbitrary labels for filtering and categorization. |
| `encrypted` | boolean | optional | `false` | Whether the content is end-to-end encrypted (NIP-44). |
| `signature` | string \| null | optional | `null` | Base64url-encoded (RFC 4648 §5, URL-safe alphabet with `=` padding) ed25519 signature over the canonical envelope. Decoders MUST accept signatures with or without padding. |

---

## Content Types

### `text/markdown`

The default content type. The `content` field is a plain string containing Markdown-formatted text suitable for human or AI consumption.

Use for: notes, summaries, project handoffs, general knowledge transfer.

### `application/aya-seed`

A conversation seed tells the receiving assistant _what to ask_, not what to know. The `content` field is a JSON object with the following structure:

| Field | Type | Required | Default | Description |
| ----- | ---- | -------- | ------- | ----------- |
| `opener` | string | **required** | — | The opening prompt or question for the receiving assistant. |
| `context_summary` | string | **required** | — | Background context the receiver needs to understand the opener. |
| `open_questions` | list\[string\] | optional | `[]` | Unresolved questions to surface to the user. |
| `expires_behavior` | string | optional | `"surface_as_reminder"` | What to do when the seed expires: `"surface_as_reminder"` or `"discard"`. |

### `application/json`

Arbitrary structured data. The `content` field is a JSON object with no enforced schema.

Use for: machine-readable payloads, structured state transfers, or custom integrations.

---

## Conflict Strategies

| Strategy | Value | Description |
| -------- | ----- | ----------- |
| Last write wins | `last_write_wins` | The newer packet replaces any conflicting local knowledge. This is the default. |
| Surface to user | `surface_to_user` | Present both the local and incoming versions and let the user decide. |
| Append | `append` | Add the incoming content alongside existing knowledge without replacing anything. |
| Skip if newer | `skip_if_newer` | Discard the incoming packet if the local version is more recent. |

---

## Signing and Verification

### Canonical serialization

The canonical form used for signing is produced by:

1. Serialize the packet with `model_dump(by_alias=True, exclude={"signature"})`.
2. If `in_reply_to` is `None`, remove it from the dict entirely (backward compatibility with pre-ACK packets).
3. Encode as JSON with `sort_keys=True, separators=(",", ":")` (compact, deterministic).
4. Convert to UTF-8 bytes.

### Signing

The sender signs the canonical bytes with their ed25519 private key (the key behind their `did:key` identity). The resulting signature is base64url-encoded and placed in the `signature` field.

### Verification

To verify a packet:

1. Extract the ed25519 public key from the `from` field's `did:key` URI:
   - Strip the `did:key:z` prefix.
   - Base58-decode the remainder.
   - Strip the 2-byte ed25519 multicodec prefix to obtain the 32-byte public key.
2. Compute the canonical bytes of the packet (as described above).
3. Base64url-decode the `signature` field.
4. Verify the ed25519 signature against the canonical bytes using the extracted public key.

---

## Wire Format

On the wire, packets are encoded as JSON objects; any valid JSON representation is acceptable.
The reference implementation serializes with `model_dump_json(by_alias=True, indent=2)` for readability, but formatting is not normative — signature verification uses canonical bytes, not the wire JSON.

**Field aliases in JSON:**

| Internal name | JSON key |
| ------------- | -------- |
| `from_did` | `from` |
| `to_did` | `to` |

All other fields use their declared names as JSON keys.

### Example: unsigned packet

```json
{
  "id": "01J5KXQZ7HBVP3N8M2RTWY6G4D",
  "version": "aya/0.2",
  "from": "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK",
  "to": "did:key:z6Mkw1KSvGWNR5e1bBV82mFHjYF5pgn8mNxLSyrFaHn4ENjm",
  "sent_at": "2026-03-30T14:30:00+00:00",
  "expires_at": "2026-04-06T14:30:00+00:00",
  "intent": "daily-sync",
  "context": "End of day project status update",
  "content_type": "text/markdown",
  "content": "## Status\n\nShipped the scheduler refactor. Tests green.",
  "reply_to": null,
  "in_reply_to": null,
  "conflict_strategy": "last_write_wins",
  "tags": ["status", "project-aya"],
  "encrypted": false,
  "signature": null
}
```

### Example: signed packet

```json
{
  "id": "01J5KXQZ7HBVP3N8M2RTWY6G4D",
  "version": "aya/0.2",
  "from": "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK",
  "to": "did:key:z6Mkw1KSvGWNR5e1bBV82mFHjYF5pgn8mNxLSyrFaHn4ENjm",
  "sent_at": "2026-03-30T14:30:00+00:00",
  "expires_at": "2026-04-06T14:30:00+00:00",
  "intent": "daily-sync",
  "context": "End of day project status update",
  "content_type": "text/markdown",
  "content": "## Status\n\nShipped the scheduler refactor. Tests green.",
  "reply_to": null,
  "in_reply_to": null,
  "conflict_strategy": "last_write_wins",
  "tags": ["status", "project-aya"],
  "encrypted": false,
  "signature": "nMdB7a1GHTqR5YvOxKw3pF-2jLkE9cZmUoAb4Xd8Ss7vQ1hNfWgRtYpKzJe0uCi3DxVm6wHbA5So8Tn2FqBCg=="
}
```

---

## Version Compatibility

The version string follows the format `aya/<major>.<minor>`.

- **Unknown minor version:** accept the packet (no warning is logged).
- **Unknown major version:** log a warning. The packet is still accepted (future versions may reject).

Current version: `aya/0.2`.

---

## Expiry

- Default expiration is 7 days after `sent_at`.
- If `expires_at` is explicitly set, that value is used instead.
- `is_expired()` returns `true` when `expires_at` is earlier than the current UTC time.
- Expired packets should be discarded by the receiving instance.
