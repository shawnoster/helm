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
header. The token is read from a host-side `.env` file alongside the
compose file via Docker Compose `env_file` and injected into the
container as the `GATEWAY_BEARER` environment variable. The file lives
on the machine running `docker compose` — it is not mounted into the
container's filesystem.

The file path is `./.env` relative to the compose file (i.e. inside
the project directory). Compose-based examples in this README,
including local docker usage, should read from `./.env` unless a
section explicitly says otherwise — **not** `/run/secrets/`, which is
tmpfs on Linux and gets wiped on every reboot.

> **Security note:** `.env` contains a secret — never commit it.
> `gateway/.env` is listed in `.gitignore`; verify with
> `git status` before every commit.

### Bootstrap (one-time, on the host)

```bash
# Pull the credential from 1Password and write it next to docker-compose.yml.
# Run this once from inside the compose directory on Babar.
cd /volume1/docker/projects/aya-gateway-compose
op read 'op://Private/aya-gateway/credential' \
  | sed 's/^/GATEWAY_BEARER=/' \
  > .env
sudo chmod 600 .env
sudo chown root:root .env
```

The file must contain a single line:

```
GATEWAY_BEARER=<your-token>
```

### Rotation

1. Update the credential in 1Password (`op://Private/aya-gateway/credential`).
2. Regenerate the `.env` file on the host (same command as Bootstrap above).
3. Restart the container to pick up the new token — DSM Container
   Manager → Container → `aya-gateway` → Restart, or via SSH:
   ```bash
   ssh babar 'docker restart aya-gateway'
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

## Local docker test

The bundled `docker-compose.yml` targets Babar — it pins `network_mode:
synobridge` (a Synology-managed bridge) and uses `env_file: ./.env`.
Neither exists on a typical dev box, so test the image locally with raw
`docker build` / `docker run` instead:

```bash
cd gateway
docker build -t aya-gateway:dev --build-arg GIT_SHA=$(git rev-parse --short HEAD) .
docker run --rm -p 8080:8080 -e GATEWAY_BEARER=dev-token aya-gateway:dev
# in another terminal:
curl localhost:8080/health
# {"ok":true,"version":"<sha>"}
```

## Deploy to Babar (production)

Babar (Synology DS224+, `192.168.50.230`) hosts the production
container, managed via **DSM Container Manager** (Projects). DSM nginx
reverse-proxies `gateway.monocularjack.com` (public HTTPS via Let's
Encrypt) to `localhost:8080` on Babar. Bearer auth is the security
primitive — see the rationale in
`notebook/projects/aya-gateway/README.md` (Ingress paths and threat
models).

### Prerequisites

- DSM admin access (web UI at `https://192.168.50.230:5001`)
- DSM Container Manager package installed (default on DSM 7.2+)
- 1Password CLI (`op`) on the dev box, signed in (used once to mint the
  bearer)
- SSH access to Babar (used once to write the host-side secrets file
  with mode 600 — DSM File Station can't set ownership/perms that the
  docker daemon needs)
- DNS for `gateway.monocularjack.com` pointing at Babar (CNAME to
  `nas-babar.duckdns.org` or A record to the public IP)
- The `synobridge` network exists on Babar — Synology auto-creates this
  on Container Manager install; no action needed

### First deploy

**1. Generate and store the bearer token**

Create an **API Credential** item in 1Password with the bearer in the
`credential` field (the Auth section reads
`op://Private/aya-gateway/credential`):

```bash
# dev box → 1Password
op item create --category="API Credential" --vault=Private \
  --title="aya-gateway" credential="$(openssl rand -base64 32)"

# Verify
op read 'op://Private/aya-gateway/credential'
```

**2. Stage the source on Babar**

The Container Manager Project needs the build context (Dockerfile,
compose, `app/`, `pyproject.toml`, `uv.lock`) on the NAS. Babar's
convention puts compose project dirs under `/volume1/docker/projects/`
(sibling to per-service data dirs like `/volume1/docker/gitea/`):

```bash
# Option A: rsync from the dev box
ssh babar 'sudo mkdir -p /volume1/docker/projects/aya-gateway-compose && sudo chown $USER:users /volume1/docker/projects/aya-gateway-compose'
rsync -av --delete \
  --exclude='__pycache__' --exclude='.venv' --exclude='*.pyc' --exclude='tests' \
  Dockerfile docker-compose.yml pyproject.toml uv.lock app \
  babar:/volume1/docker/projects/aya-gateway-compose/
```

```
Option B: DSM File Station
  1. In /volume1/docker/projects/, create folder aya-gateway-compose
  2. Drag-and-drop Dockerfile, docker-compose.yml, pyproject.toml,
     uv.lock, and the app/ directory into it
```

**3. Write the .env file on Babar**

The compose loads secrets from `./.env` (relative to the compose file,
i.e. `/volume1/docker/projects/aya-gateway-compose/.env`). `op` runs on
the dev box (see Prerequisites); the token is obtained there first, then
written to Babar in an interactive root shell.

First, read the token on the dev box and copy it to the clipboard:

```bash
# dev box
op read 'op://Private/aya-gateway/credential'
```

Then open an interactive root shell on Babar and write the file.
Single-quoting the token prevents shell interpretation of `+`, `/`, or
`=` characters in base64 tokens:

```bash
# dev box → Babar (interactive; DSM requires a terminal for sudo password)
ssh -t babar
sudo -i
cd /volume1/docker/projects/aya-gateway-compose
printf 'GATEWAY_BEARER=%s\n' 'PASTE_TOKEN_HERE' > .env
chmod 600 .env
chown root:root .env
stat -c '%a %U %G %n' .env
# Expected: 600 root root .env
exit  # leave root shell
exit  # leave ssh
```

**4. Create the Project in Container Manager (DSM web UI)**

Open DSM at `https://192.168.50.230:5001` →
**Container Manager → Project → Create**:

| Field | Value |
|---|---|
| Project name | `aya-gateway` |
| Path | `/volume1/docker/projects/aya-gateway-compose` |
| Source | **Use existing docker-compose.yml** (the one staged in step 2; DSM auto-detects `compose.yml` / `compose.yaml` / `docker-compose.yml`) |
| Build | Enable (Container Manager will run `docker compose up --build`) |

The compose pins `image: aya-gateway:latest`; Container Manager builds
that tag from the local Dockerfile on first deploy and on every rebuild.

**Optional — embed the git sha as the version string.** The compose
references `${GIT_SHA:-unknown}` as a build arg. To get the sha into
`/health`'s `version` field, edit the compose on Babar before creating
the Project and replace `${GIT_SHA:-unknown}` with the literal short
sha (`git rev-parse --short HEAD` on the dev box). Otherwise `version`
reports `unknown` — harmless, just less informative.

**5. Smoke test on Babar**

After the Project shows **Running**, from any LAN host:

```bash
curl -sf http://192.168.50.230:8080/health
# {"ok":true,"version":"..."}
```

If it fails, inspect logs in **Container Manager → Container →
aya-gateway → Log** (or via SSH: `ssh babar 'docker logs aya-gateway'`).

The most common first-deploy failure is `RuntimeError: GATEWAY_BEARER is
not set` — the secrets file is missing, has the wrong path, or isn't
readable by DSM Container Manager (or the account running `docker compose`).

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

**Tail logs** — DSM Container Manager → Container → `aya-gateway` →
Log, or via SSH:

```bash
ssh babar 'docker logs -f --tail=50 aya-gateway'
```

**Update (after pushing changes to main)** — re-stage source, then
rebuild via the UI:

```bash
# 1. Re-stage source on Babar
rsync -av --delete \
  --exclude='__pycache__' --exclude='.venv' --exclude='*.pyc' --exclude='tests' \
  Dockerfile docker-compose.yml pyproject.toml uv.lock app \
  babar:/volume1/docker/projects/aya-gateway-compose/
```

```
# 2. DSM Container Manager → Project → aya-gateway → Build → Run
#    (Container Manager rebuilds the image and recreates the container)
```

**Stop / start** — Container Manager → Project → `aya-gateway` →
Action menu (Stop, Start, Clean).

**Token rotation** — see "Rotation" under Auth, above. After
regenerating `.env` on Babar, restart the container via Container
Manager (the env_file is re-read on container start).

### Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Container won't start, `RuntimeError: GATEWAY_BEARER is not set` | `.env` file missing, wrong path, or unreadable by DSM Container Manager (or by the user running `docker compose`) | `ssh babar 'sudo ls -l /volume1/docker/projects/aya-gateway-compose/.env'` — must exist, mode 600, and be readable by DSM Container Manager (or the account running `docker compose`) |
| DSM Project YAML linter rejects `env_file:` block | List form (`env_file:` then `- ./.env` on next line) is finicky in DSM's editor | use the inline string form: `env_file: ./.env` |
| `curl localhost:8080/health` on Babar returns connection refused | container not running or crashed at startup | Container Manager → Container → `aya-gateway` → Log, or `ssh babar 'docker logs aya-gateway'` |
| DSM reverse proxy returns 502 | container down OR port mismatch in DSM rule | verify `localhost:8080` in the DSM rule + check Container Manager → Project status |
| Project build fails with `network synobridge not found` | Synology Docker package not initialized (synobridge is auto-created on install) | install / re-init Container Manager via Package Center |
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
├── docker-compose.yml   # synobridge networking, restart always (Babar)
└── pyproject.toml       # fastapi, uvicorn + dev tooling
```
