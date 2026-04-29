# aya-gateway

Personal HTTP gateway — phone capture, local effects, future webhook
receivers. Lives in the aya monorepo as a sibling to the CLI; full
design and roadmap at `notebook/projects/aya-gateway/README.md`.

## Status

Phase 0 — bootstrap. `/health` only. Auth, deploy, business routes
follow in [shawnoster/aya issues with the `gateway` label](https://github.com/shawnoster/aya/issues?q=is%3Aissue+label%3Agateway).

## Quickstart (local dev)

```bash
cd gateway
uv sync
uv run uvicorn app.main:app --reload --port 8080
curl localhost:8080/health
# {"ok":true,"version":"dev"}
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
│   └── main.py          # FastAPI app + /health
├── tests/
│   └── test_health.py
├── Dockerfile           # multi-stage, non-root, Python 3.12
├── docker-compose.yml   # bridge networking, restart unless-stopped
└── pyproject.toml       # fastapi, uvicorn + dev tooling
```
