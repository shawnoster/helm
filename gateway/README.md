# aya-gateway

Personal HTTP gateway — phone capture, local effects, future webhook
receivers. Lives in the aya monorepo as a sibling to the CLI; full
design and roadmap at `notebook/projects/aya-gateway/README.md`.

## Status

Phase 0 — bootstrap + auth + deploy. `/health` is unauthed (liveness
probes); all other routes require `Authorization: Bearer <token>`.
Hosted on Babar (Synology DS224+), reverse-proxied at
`https://gateway.monocularjack.com` via DSM nginx. Business routes
land in subsequent phases.

## Auth

All routes except `/health` require an `Authorization: Bearer <token>`
header. The token is read from a host-side secrets file at
`/run/secrets/gateway.env` via Docker Compose `env_file` and injected
into the container as the `GATEWAY_BEARER` environment variable. The
file lives on the machine running `docker compose` — it is not mounted
into the container's filesystem.

### Bootstrap (one-time, on the host)

```bash
# Pull the credential from 1Password and write it to the secrets file.
# Run this once before starting the container.
op read 'op://Private/aya-gateway/credential' \
  | sed 's/^/GATEWAY_BEARER=/' \
  > /run/secrets/gateway.env
chmod 600 /run/secrets/gateway.env
```

The file must contain a single line:

```
GATEWAY_BEARER=<your-token>
```

### Rotation

1. Update the credential in 1Password (`op://Private/aya-gateway/credential`).
2. Regenerate the secrets file on the host:
   ```bash
   op read 'op://Private/aya-gateway/credential' \
     | sed 's/^/GATEWAY_BEARER=/' \
     > /run/secrets/gateway.env
   chmod 600 /run/secrets/gateway.env
   ```
3. Restart the container to pick up the new token:
   ```bash
   docker compose restart
   ```

## Quickstart (local dev)

```bash
cd gateway
GATEWAY_BEARER=dev-token uv run uvicorn app.main:app --reload --port 8080
curl localhost:8080/health
# {"ok":true,"version":"dev"}
```

Auth is enforced for non-`/health` routes, but no authenticated
application endpoint is exposed yet in this phase. Future routes will
go on the `authenticated` router in `app/main.py`.

Run the test suite:

```bash
uv run pytest
uv run ruff check .
uv run mypy app
```

## Quickstart (docker)

```bash
cd gateway
# Create the secrets file first (see Bootstrap above), then:
docker compose up -d --build
curl localhost:8080/health
docker compose down
```

For a versioned build, pass the git sha:

```bash
GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build
curl localhost:8080/health
# {"ok":true,"version":"<sha>"}
```

## Deploy to Babar (production)

Babar (Synology DS224+, `192.168.50.230`) hosts the production
container. DSM nginx reverse-proxies `gateway.monocularjack.com`
(public HTTPS via Let's Encrypt) to `localhost:8080` on Babar. Bearer
auth is the security primitive — see the rationale in
`notebook/projects/aya-gateway/README.md` (Ingress paths and threat
models).

### Prerequisites

- SSH access to Babar (`ssh babar`, configured via 1Password)
- 1Password CLI (`op`) on the dev box, signed in
- DNS for `gateway.monocularjack.com` pointing at Babar (CNAME to
  `nas-babar.duckdns.org` or A record to the public IP)
- DSM admin access (web UI at `https://192.168.50.230:5001`)

### First deploy

Run all `dev box →` commands from this repo's `gateway/` directory.

**1. Generate and store the bearer token**

The existing Auth section reads from `op://Private/aya-gateway/credential`, so
create an **API Credential** item with the bearer in the `credential`
field:

```bash
# dev box → 1Password
op item create --category="API Credential" --vault=Private \
  --title="aya-gateway" credential="$(openssl rand -base64 32)"

# Verify
op read 'op://Private/aya-gateway/credential'
```

**2. Create the deploy directory on Babar**

```bash
# dev box → babar
ssh babar 'sudo mkdir -p /volume1/docker/aya-gateway && sudo chown $USER:users /volume1/docker/aya-gateway'
```

**3. Copy compose + Dockerfile + app to Babar**

```bash
# dev box
rsync -av --delete \
  --exclude='__pycache__' --exclude='.venv' --exclude='*.pyc' \
  Dockerfile docker-compose.yml pyproject.toml uv.lock app \
  babar:/volume1/docker/aya-gateway/
```

**4. Write the secrets file on Babar**

The compose file expects the secrets at `/run/secrets/gateway.env` on
the host. `op read` only runs on the dev box that's signed in; the
token is streamed over SSH so it never appears on a command line or in
shell history:

```bash
ssh babar 'sudo mkdir -p /run/secrets'
{ printf 'GATEWAY_BEARER='; \
  op read 'op://Private/aya-gateway/credential'; \
  printf '\n'; } \
  | ssh babar 'sudo tee /run/secrets/gateway.env >/dev/null && sudo chmod 600 /run/secrets/gateway.env'

# Verify presence + permissions without echoing the secret:
ssh babar "sudo test -s /run/secrets/gateway.env && sudo stat -c '%a %U %G %n' /run/secrets/gateway.env"
# Expected: 600 root root /run/secrets/gateway.env
```

**5. Build and start the container**

```bash
# GIT_SHA expands locally on the dev box (Babar has no aya checkout)
ssh babar "cd /volume1/docker/aya-gateway && GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build"
```

**6. Smoke test on Babar**

```bash
ssh babar 'curl -sf localhost:8080/health'
# {"ok":true,"version":"..."}
```

If this fails, check container logs:

```bash
ssh babar 'cd /volume1/docker/aya-gateway && docker compose logs gateway'
```

The most common first-deploy failure is `RuntimeError: GATEWAY_BEARER is
not set` — the env_file path is wrong or the file isn't readable by the
docker daemon.

### DSM reverse proxy (browser, one-time)

Open DSM at `https://192.168.50.230:5001`.

1. **Control Panel → Login Portal → Advanced → Reverse Proxy → Create**
2. **General**:
   - Description: `aya-gateway`
   - Source — Protocol: `HTTPS`, Hostname: `gateway.monocularjack.com`, Port: `443`
   - Destination — Protocol: `HTTP`, Hostname: `localhost`, Port: `8080`
3. **Custom Header → Create → WebSocket** (not strictly needed yet,
   but consistent with the relay rule and future-proof)
4. **Save**

### Let's Encrypt cert (browser, one-time)

DNS for `gateway.monocularjack.com` must already point at Babar before
this step (LE validates via HTTP-01 against port 80 on the public IP).

1. **Control Panel → Security → Certificate → Add → Add a new certificate**
2. **Get a certificate from Let's Encrypt** → Next
3. Domain name: `gateway.monocularjack.com`
4. Email: your address
5. **Apply** — DSM provisions the cert and auto-renews thereafter
   (mandatory in DSM 7+, no toggle exists; see
   `notebook/knowledge/synology.md`).

### End-to-end smoke tests

Run after DSM proxy + LE are configured:

```bash
# 1. From Babar (loopback, no DSM in path)
ssh babar 'curl -sf localhost:8080/health'

# 2. From any LAN host (HA's expected path)
curl -sf http://192.168.50.230:8080/health

# 3. From iPhone over LTE — disconnect WiFi first
curl -sf https://gateway.monocularjack.com/health
# {"ok":true,"version":"..."}

# 4. From iPhone over LTE without a token (verify auth applies once
#    protected routes ship — currently /health is intentionally open
#    and there are no other defined routes to test)
curl -i https://gateway.monocularjack.com/some-future-route
# HTTP/2 401  ← once a protected route exists
# HTTP/2 404  ← currently, since no other routes are defined yet
```

The 401 test becomes meaningful once Phase 1 (`POST /effects/kitt`) or
Phase 2 (`POST /inbox`) lands. For Phase 0, a public 200 on `/health`
plus a LAN 200 on `:8080/health` is the deploy contract.

### Routine ops

```bash
# Tail logs
ssh babar 'cd /volume1/docker/aya-gateway && docker compose logs -f --tail=50'

# Update (after pushing changes to main)
rsync -av --delete \
  --exclude='__pycache__' --exclude='.venv' --exclude='*.pyc' \
  Dockerfile docker-compose.yml pyproject.toml uv.lock app \
  babar:/volume1/docker/aya-gateway/
ssh babar "cd /volume1/docker/aya-gateway && GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build"

# Token rotation — see "Rotation" under Auth, above. Run on Babar.

# Stop
ssh babar 'cd /volume1/docker/aya-gateway && docker compose down'
```

### Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Container won't start, `RuntimeError: GATEWAY_BEARER is not set` | secrets file missing or not readable by docker daemon | `ssh babar 'sudo ls -l /run/secrets/gateway.env'` — must exist, mode 600, owner readable by docker |
| `curl localhost:8080/health` on Babar returns connection refused | container not running or crashed at startup | `ssh babar 'cd /volume1/docker/aya-gateway && docker compose logs gateway'` |
| DSM reverse proxy returns 502 | container down OR port mismatch in DSM rule | verify `localhost:8080` in the DSM rule + check container status with `ssh babar 'cd /volume1/docker/aya-gateway && docker compose ps'` |
| LE cert acquisition fails | DNS not pointing at Babar yet, or LE rate limit (5 certs/week/domain) | verify `dig gateway.monocularjack.com` returns the public IP; if rate limited, wait it out |
| Public `https://gateway.../health` hangs | DSM nginx not running, or port 443 not forwarded | check DSM Web Station, verify port-forward at router |

## Layout

```
gateway/
├── app/
│   ├── __init__.py
│   └── main.py          # FastAPI app, /health, bearer-auth dependency
├── tests/
│   ├── conftest.py      # sets GATEWAY_BEARER for the test run
│   ├── test_auth.py     # bearer-token auth tests
│   └── test_health.py   # /health smoke tests
├── Dockerfile           # multi-stage, non-root, Python 3.12
├── docker-compose.yml   # bridge networking, restart unless-stopped
└── pyproject.toml       # fastapi, uvicorn + dev tooling
```
