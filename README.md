# ETOERAGCB

A closed-registration, multi-tenant RAG assistant with a FastAPI backend,
SvelteKit frontend, versioned ingestion, hybrid PostgreSQL/Qdrant retrieval,
optional isolated web evidence, local model serving, citation-safe SSE, and
hardened Docker deployment.

The deployment remains LAN-only pending the public ACME/external isolation and
final host-reboot gates. Caddy is the only published service.

## Current architecture

- `frontend/`: TypeScript SvelteKit 2 / Svelte 5 application and server-side API
  gateway. Browser code never receives FastAPI bearer or refresh tokens.
- `backend/`: Python 3.13 FastAPI API, ARQ worker, ingestion, retrieval,
  generation, evaluation, and operations.
- `deploy/`: hardened Compose, Caddy, monitoring, backup, audit, and release
  tooling.
- `docs/`: phase contracts. P12 is the SvelteKit migration; P9 remains
  historical evidence for the retired Streamlit client.

Sessions use an AES-256-GCM encrypted `__Host-rag_session` cookie. Document
citations open an authorized in-app plain-text preview; the UI does not request
signed files or downloads.

See [architecture.md](architecture.md), [adminworks.md](adminworks.md),
[docs/deployment.md](docs/deployment.md), and
[docs/p12-sveltekit.md](docs/p12-sveltekit.md).

## Verification

Backend:

```bash
cd backend
uv sync --frozen --all-groups
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run pytest
```

Frontend:

```bash
cd frontend
npm ci
npm run format:check
npm run lint
npm run check
npm test
npm run build
npm run test:e2e
npm audit --audit-level=high
```

Deployment boundary:

```bash
python3 scripts/verify_compose_boundary.py
docker compose --env-file deploy/.env -f deploy/compose.yml config --quiet
```

## Start the stack

Copy the appropriate `deploy/.env*.example`, create every file documented in
`deploy/secrets/README.md` (including the independent
`frontend_session_secret`), then:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml up -d --build
```

Only Caddy publishes TCP 80/443. Do not expose frontend, backend, data, model,
or search service ports.
