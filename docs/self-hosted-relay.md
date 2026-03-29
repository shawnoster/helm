# Self-Hosted Nostr Relay on Synology NAS

Run your own Nostr relay on a Synology NAS so aya's work ↔ home packet sync
doesn't depend on public relays. A LAN-local relay is faster, always available,
and keeps your packets off infrastructure you don't control.

## Why bother?

Public relays (nos.lol, relay.damus.io) work, but:

- They go down or 503 intermittently
- Propagation between relays is async and can take seconds to minutes
- You have no control over retention or authentication
- Your packets (even encrypted) are visible to relay operators

A self-hosted relay on a NAS you already own costs nothing extra and gives you
sub-second delivery on your LAN, plus the option to lock it down to your
keypairs only via NIP-42 authentication.

---

## Relay software

**Recommended: [`nostr-rs-relay`](https://github.com/scsibug/nostr-rs-relay)**

- Single binary, low memory footprint
- SQLite backend — fine for personal use
- Supports NIP-42 (auth), NIP-11 (relay info), NIP-09 (event deletion)
- Official Docker image: `scsibug/nostr-rs-relay`

**Alternative: [`strfry`](https://github.com/hoytech/strfry)**

- Higher throughput, LMDB backend
- More complex config, better for high-volume use
- Use this if you want stream sync between two relays

For a single-user aya setup, `nostr-rs-relay` is the right call.

---

## Prerequisites

- Synology NAS running DSM 7.x
- **Container Manager** installed (Package Center → Container Manager)
- A domain or DDNS hostname pointing to your NAS (for TLS + remote access)
  - Synology DDNS works: `yourname.synology.me`
  - Or use your own domain with a CNAME to the DDNS host
- Ports 80 and 443 forwarded to your NAS in your router (for Let's Encrypt)
- Port 7777 or 8008 available internally (relay WebSocket port)

---

## Step 1 — Create the config file

SSH into your NAS or use File Station to create the following directory structure:

```
/volume1/docker/nostr-relay/
  config.toml
  data/          ← SQLite DB lands here (auto-created)
```

Create `/volume1/docker/nostr-relay/config.toml`:

```toml
[info]
relay_url = "wss://nostr.yourdomain.com"
name = "My aya relay"
description = "Private relay for aya packet sync"
contact = "you@example.com"

[database]
# Path inside the container — maps to /volume1/docker/nostr-relay/data on host
data_directory = "/usr/src/app/db"

[network]
port = 8080
address = "0.0.0.0"

[limits]
# Keep these low for a personal relay
messages_per_sec = 10
max_event_bytes = 131072     # 128 KB — packets are small
max_ws_message_bytes = 131072

[authorization]
# Populate pubkey_whitelist to require NIP-42 auth — see "Lock down to your keys" below
pubkey_whitelist = []        # empty = open relay; add hex pubkeys to restrict

[retention]
# Keep events for 90 days — personal relay, low volume
max_age_days = 90
```

---

## Step 2 — Create the Docker Compose file

In Container Manager → Project → Create, paste this compose config.

Or save to `/volume1/docker/nostr-relay/docker-compose.yml` and import:

```yaml
services:
  nostr-relay:
    image: scsibug/nostr-rs-relay:latest
    container_name: nostr-relay
    restart: unless-stopped
    ports:
      - "8008:8080"        # host:container — relay WebSocket on port 8008
    volumes:
      - /volume1/docker/nostr-relay/config.toml:/usr/src/app/config.toml:ro
      - /volume1/docker/nostr-relay/data:/usr/src/app/db
    environment:
      - RUST_LOG=info
```

Start the container. Check logs — you should see:

```
[INFO] listening on 0.0.0.0:8080
```

---

## Step 3 — Reverse proxy + TLS

Use the DSM built-in reverse proxy to put TLS in front of the relay.

### DSM Control Panel → Login Portal → Advanced → Reverse Proxy

Create a new rule:

| Field | Value |
| ---- | ---- |
| Source protocol | HTTPS |
| Source hostname | `nostr.yourdomain.com` |
| Source port | 443 |
| Destination protocol | HTTP |
| Destination hostname | `localhost` |
| Destination port | `8008` |

**Critical: enable WebSocket upgrade.** Under the rule → Custom Header → Create:

| Header name | Value |
| ---- | ---- |
| `Upgrade` | `$http_upgrade` |
| `Connection` | `Upgrade` |

Without these headers the WebSocket handshake will fail.

### TLS certificate

DSM Control Panel → Security → Certificate → Add → Get from Let's Encrypt.

Add `nostr.yourdomain.com` as the domain. Assign the certificate to your
reverse proxy rule.

---

## Step 4 — Verify the relay

Test the WebSocket connection from your workstation:

```bash
# Quick connectivity check
curl -i https://nostr.yourdomain.com

# Should return NIP-11 relay info JSON if the relay is up
# {"name":"My aya relay","description":"Private relay for aya packet sync",...}
```

Test with aya:

```bash
# Check inbox via your relay
aya inbox --relay wss://nostr.yourdomain.com

# Send a test dispatch through your relay
echo "relay test" | aya dispatch \
  --to home \
  --intent "self-hosted relay test" \
  --relay wss://nostr.yourdomain.com

# Receive via your relay on the other machine
aya receive --relay wss://nostr.yourdomain.com
```

---

## Step 5 — Configure aya to use your relay

Edit `~/.aya/profile.json` and update `default_relays`:

```json
{
  "aya": {
    "default_relays": [
      "wss://nostr.yourdomain.com",
      "wss://nos.lol"
    ]
  }
}
```

Put your relay first — aya publishes to all configured relays and queries each
one in order with deduplication, so the first entry is simply tried first. Keep
`nos.lol` second as a fallback for when you're away from home and the NAS isn't
reachable.

Both instances (work and home) need the same update.

---

## Optional: lock down to your keypairs (NIP-42)

To reject events from anyone except your own instances, add your Nostr pubkeys
to the whitelist in `config.toml`:

```toml
[authorization]
pubkey_whitelist = [
  "393c068faf7cb22d92b02d84d348b64e76b0afaca7de6e37a7c6de69626a8c5e",  # work
  "58237874cd5623e1a89075d32119fbd2c15ef20cb82f636960dea44bcaf80db3",   # home
]
```

Get your pubkeys from:

```bash
python3 -c "
import json, pathlib
p = json.loads(pathlib.Path('~/.aya/profile.json').expanduser().read_text())
for label, inst in p['aya']['instances'].items():
    print(label, inst['nostr_public_hex'])
for label, key in p['aya']['trusted_keys'].items():
    print(label, key.get('nostr_pubkey', '(no nostr key)'))
"
```

Restart the container after changing `config.toml`:

```bash
# In Container Manager, or via SSH:
docker restart nostr-relay
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| ---- | ---- | ---- |
| `curl https://nostr.yourdomain.com` returns 502 | Container not running or wrong port | Check Container Manager logs; verify port 8008 is mapped |
| WebSocket connection refused | Missing Upgrade headers in reverse proxy | Add `Upgrade` and `Connection` custom headers |
| `aya inbox` shows nothing | `last_checked` window — packets older than cursor | Run `aya inbox --relay wss://nostr.yourdomain.com` directly |
| Let's Encrypt cert fails | Port 80/443 not forwarded to NAS | Check router port forwarding; verify DDNS resolves to your WAN IP |
| NIP-42 auth rejected | Pubkey not in whitelist | Add hex pubkey to `pubkey_whitelist` in config.toml and restart |
| Works on LAN, fails remotely | NAS firewall blocking 443 | DSM Control Panel → Security → Firewall → allow 443 from WAN |

---

## LAN-only setup (no domain, no TLS)

If you only want relay sync on your home network and don't need remote access:

1. Skip the reverse proxy and Let's Encrypt steps
2. Connect directly to the relay on HTTP: `ws://nas-ip:8008`
3. Update `default_relays` on both machines to `ws://192.168.1.x:8008`

Note: aya uses standard WebSocket — `ws://` (no TLS) works fine on a trusted
LAN. Only use `wss://` if traffic crosses the internet.
