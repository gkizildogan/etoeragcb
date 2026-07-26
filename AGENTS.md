# Repository Guidelines

## Project Structure & Module Organization

The FastAPI service lives in `backend/app/`, organized by domain (`auth/`,
`documents/`, `ingest/`, `rag/`, and `chat/`); Alembic migrations are under
`backend/alembic/`. The SvelteKit client and server-side API gateway are in
`frontend/`. Tests live in `backend/tests/`, `frontend/src/**/*.test.ts`, and
`frontend/tests/e2e/`.
Deployment and operational files are in `deploy/`; shared utilities are in
`scripts/`. Consult `docs/` and `architecture.md` before changing system
boundaries.

## Build, Test, and Development Commands

Use Python 3.13 and `uv`; dependencies are fully pinned.

- `cd backend && uv sync --frozen --all-groups` installs backend dependencies.
- `cd backend && uv run ruff check . && uv run ruff format --check .` runs CI
  lint and formatting checks.
- `cd backend && uv run mypy app && uv run pytest` performs strict type checking
  and runs backend tests.
- `cd frontend && npm ci && npm run format:check && npm run lint && npm run check`
  installs and statically verifies the UI.
- `cd frontend && npm test && npm run build && npm run test:e2e` runs unit,
  component, production-build, and Playwright checks.
- `python3 scripts/verify_compose_boundary.py` validates deployment isolation.
- `docker compose --env-file deploy/.env -f deploy/compose.yml up -d --build`
  builds and starts the complete stack after local configuration.

## Coding Style & Naming Conventions

Use four-space indentation, modern Python 3.13 syntax, explicit type annotations,
and a 100-character line limit. Ruff enforces imports, security checks, async
patterns, and common bug rules; backend mypy runs in strict mode. Name modules
and functions `snake_case`, classes `PascalCase`, and constants `UPPER_SNAKE_CASE`.
Keep API routes thin and place business logic in the relevant domain service.

## Testing Guidelines

Pytest discovers `test_*.py`; test functions should describe behavior, for
example `test_rejects_unsafe_or_unqualified_values`. Backend async tests use
`pytest-asyncio` in auto mode. Add regression tests with every bug fix and cover
tenant isolation, authorization, failure paths, and configuration validation
when relevant. Run the full suite for each modified component.

## Commit & Pull Request Guidelines

History uses short, capitalized, scope-oriented subjects such as `P11 Updates`
and `Hot-fix for document ingestion`. Keep commits focused and identify the
phase or component. Pull requests should explain behavior and operational
impact, link issues or phase documents, list verification commands, and include
screenshots for visible SvelteKit changes. Ensure all CI checks pass.

## Security & Configuration

Never commit `deploy/.env`, secret files, uploaded documents, model caches, or
generated artifacts. Start from the committed `.env.example` files, preserve
pinned versions and image digests, and avoid exposing internal service ports.
