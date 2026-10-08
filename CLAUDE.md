# CLAUDE.md

Guidance for Claude Code when working in this repository. Contributor rules
(style, commit/PR conventions, security hygiene) live in `AGENTS.md` and apply
here too:

@AGENTS.md

## What this is

ETOERAGCB is a closed-registration, multi-tenant RAG assistant: a FastAPI
backend and ARQ ingestion worker, a SvelteKit 2 / Svelte 5 frontend that acts as
a backend-for-frontend, PostgreSQL + Qdrant + Redis for data, local vLLM/TEI
model serving, optional isolated web search, and a hardened Docker Compose
deployment fronted by Caddy. The deployment is LAN-only for now.

Read `architecture.md` before changing system boundaries or the RAG pipeline; it
contains a line-referenced control map of retrieval and generation. `README.md`
has the verification matrix, `adminworks.md` is the operator runbook, and
`docs/` holds per-phase contracts (P3-P12). `rag-chatbot-plan.md` and
`docs/p9-streamlit.md` are historical.

## Commands

Backend (Python 3.13, `uv`, run from `backend/`):

```bash
uv sync --frozen --all-groups
uv run ruff check . && uv run ruff format --check .
uv run mypy app                       # strict
uv run pytest                         # all tests
uv run pytest tests/test_p6.py -k name   # single file / test
uv run python -m app.evaluation.cli verify   # CI also verifies committed calibration
```

Frontend (Node >= 24.18.0, run from `frontend/`):

```bash
npm ci
npm run format:check && npm run lint && npm run check
npm test                              # vitest (single: npx vitest run src/lib/sse.test.ts)
npm run build && npm run test:e2e     # Playwright against tests/mock-api.mjs
```

Deployment: `python3 scripts/verify_compose_boundary.py` (needs `docker compose`)
and `docker compose --env-file deploy/.env -f deploy/compose.yml config --quiet`.
CI (`.github/workflows/ci.yml`) runs all of the above plus dependency and image
audits, so run the full suite for any component you modify. Backend tests need no
running services (they use `aiosqlite` and fixtures in `backend/tests/conftest.py`).

## Architecture essentials

- **PostgreSQL is authoritative** for identity, tenancy, document visibility,
  ingestion state, active index generations, and chat history. Qdrant is a derived
  index that only proposes chunk IDs; candidates are re-hydrated from PostgreSQL
  under active tenant/version scope. Redis is disposable (queues, rate limits,
  version-bound caches).
- **Tenancy is enforced in the backend**, never trusted from SvelteKit. Every
  query must stay principal- and tenant-scoped. Model-provided hints (planner
  output) may boost ranking but must never grant access.
- **Browser never sees backend tokens.** SvelteKit keeps bearer/refresh tokens in
  an AES-256-GCM `__Host-rag_session` cookie and proxies via a strict `/ui-api/*`
  allowlist (handlers in `frontend/src/routes/ui-api/`, gateway in
  `frontend/src/lib/server/`). New browser-facing endpoints must go through that
  gateway and keep generic, non-leaking error messages.
- **Backend layout** (`backend/app/`): domain packages `auth/`, `sessions/`,
  `documents/`, `collections/`, `ingest/`, `rag/`, `chat/`, `web/`, `operations/`,
  `evaluation/`; `models/` for SQLAlchemy, `core/` for db/logging/metrics,
  `config.py` for settings, `main.py` for app wiring. Routes stay thin; logic goes
  in services. Migrations: `backend/alembic/versions/` (sequential `000N_*.py`).
- **Chat path:** `chat/orchestrator.py` -> `rag/planner.py` -> `rag/scope.py` ->
  `rag/retriever.py` (dense + sparse, RRF) -> optional `rag/web.py` -> reranker ->
  dedup -> confidence filter -> context pack -> calibrated gate
  (`rag/calibration/retrieval_gate.v1.json`) -> generation -> citation repair ->
  atomic persistence. SSE events: `start`, `status`, `delta`, `replace`,
  `citations`, `done`, `error`.
- **Index-affecting changes are migrations.** Changing chunking, embedding model or
  dimension, sparse hashing, or Qdrant payloads requires reindexing, not just a
  query-time edit. Changing retrieval thresholds means recalibrating
  (`app.evaluation`), not hand-tuning.
- Arbitrary URL fetching belongs only in the isolated `web-fetcher` deployment,
  not in the backend container.

## Deployment and pinning rules

- Caddy is the only published service (TCP 80/443). Do not publish other ports or
  weaken network isolation (`edge`, `data`, `model`, `search`, and egress-only
  networks); `scripts/verify_compose_boundary.py` enforces this.
- Containers use read-only roots, dropped capabilities, `no-new-privileges`, and
  resource limits; keep them when editing `deploy/compose.yml`.
- Everything is pinned: backend deps (`uv.lock`, `requirements*.lock` with hashes),
  frontend deps (exact versions + `package-lock.json`), images with digests
  (`docker-images.lock`), and model revisions (`model-revisions.lock`). Do not
  loosen pins or use floating tags; update tag and digest together.
- SvelteKit owns the HTML CSP (nonce mode); don't let Caddy override it.
- Never commit `deploy/.env`, `deploy/secrets/*`, `model-cache/`, uploaded
  documents, or `deploy/state/*`. Start from the `.env*.example` files.

## Testing expectations

- Add a regression test with every bug fix; cover tenant isolation, authorization,
  failure paths, and config validation where relevant. Backend tests are named by
  behavior (`test_rejects_unsafe_or_unqualified_values`) and many are grouped by
  phase (`test_p4.py` ingestion, `test_p5.py` retrieval, `test_p6.py` post-retrieval,
  `test_p7.py` web retrieval, `test_p8.py` generation/chat, `test_p11.py` operations).
- Frontend unit tests sit beside sources (`*.test.ts`); browser flows are in
  `frontend/tests/e2e/` against the deterministic mock API.
- Ruff selects `E,F,I,UP,B,ASYNC,S,RUF` (assert allowed); mypy is strict with the
  pydantic plugin. Fix findings rather than adding `noqa`/`type: ignore` unless
  genuinely justified.
