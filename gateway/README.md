# aya-gateway

Personal HTTP gateway — phone capture, local effects, future webhook
receivers. Lives in the aya monorepo as a sibling to the CLI; full
design and roadmap at `notebook/projects/aya-gateway/README.md`.

## Status

Phase 1 — bearer-token auth. `/health` is unauthed (liveness probes);
all other routes require `Authorization: Bearer <token>`. Deploy and
business routes follow in subsequent issues.

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
curl -H "Authorization: Bearer dev-token" localhost:8080/
# future authenticated routes return 200
```

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
