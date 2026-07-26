# P12 SvelteKit migration

P12 replaces the P9 Streamlit client with a TypeScript SvelteKit application.
FastAPI, PostgreSQL, Redis, Qdrant, workers, model services, retrieval, and the
public bearer-token API remain authoritative and compatible.

## Delivered scope

- Node 24.18.0, Svelte 5.56.8, SvelteKit 2.70.1, and adapter-node 5.5.7
- exact direct dependency pins, `package-lock.json`, and `npm ci`
- strict TypeScript, ESLint, Prettier, svelte-check, Vitest/Testing Library,
  production build, and Playwright
- `/login`, `/chat`, `/documents`, and `/collections`
- responsive desktop sidebar and mobile drawer
- login/logout, password-confirmed tenant switching, role controls
- private session CRUD, scoped chat, optional web search, SSE/replay, feedback
- document upload/version polling/reindex/delete
- collection CRUD and document membership
- authorized plain-text document citation previews
- hardened adapter-node container and Caddy gateway routing

## Authentication boundary

SvelteKit is a backend-for-frontend. Browser code calls only same-origin pages,
actions, and `/ui-api/*`. Bearer and refresh tokens are stored in the versioned
payload of `__Host-rag_session`, encrypted/authenticated with AES-256-GCM using
an independent 32-byte Docker secret.

Cookie flags are `Secure`, `HttpOnly`, `SameSite=Lax`, and `Path=/`. SvelteKit
retries one FastAPI 401 after rotating refresh tokens, updates the cookie, and
clears it after hard expiry or logout.

Concurrent refreshes are deduplicated by an old refresh-token SHA-256 hash. A
bounded, ten-second old-token mapping returns the same rotated result to
simultaneous requests, avoiding FastAPI reuse detection. Token values and
passwords are never logged or sent to browser JavaScript.

Tenant switching authenticates the target membership with the user's password,
revokes the old refresh token best-effort, replaces the cookie, and reloads
tenant-scoped data. P9 sessions are intentionally not migrated.

## Citation preview API

```text
GET /api/messages/{message_id}/citations/{source_id}/preview
```

Response:

```json
{
  "message_id": "uuid",
  "source_id": "S1",
  "title": "Document title",
  "source_filename": "handbook.pdf",
  "page_start": 2,
  "page_end": 2,
  "text": "Exact retrieved chunk text"
}
```

The backend:

1. loads only an assistant message owned by the active user and tenant;
2. resolves `[source_id]` through both the stored citation map and retained
   retrieval sources;
3. accepts only a document source whose candidate is a PostgreSQL chunk UUID;
4. cross-checks tenant, document, version, content hash, and retained
   section/chunk identifiers;
5. requires a non-deleted document and a ready/active/superseded,
   non-garbage-collected version;
6. returns exact `Chunk.text_original` and `Cache-Control: private, no-store`.

Malformed IDs, web citations, cross-user/cross-tenant access, deleted or
collected data, missing chunks, and mismatches all return generic 404.
No migration is required because assistant metadata already retained the chunk
candidate UUID and provenance.

The SvelteKit UI fetches only this JSON. Its plain-text dialog closes by button,
Escape, or backdrop and restores focus. Signed-file APIs remain compatible but
are not called by the UI. Web citations remain validated external links; web
excerpts are neither persisted nor previewed.

## SSE rules

The client accepts `start`, `status`, `delta`, `replace`, `citations`, `done`,
and `error`, ignores unknown events, and treats `replace` as authoritative.
Fragmented lines/data are supported. A missing terminal event or transport
failure reconnects once with the identical body, UUID, and idempotency key.
FastAPI replay then produces the stored authoritative transcript.

## Container and ingress

The frontend image is a multi-stage build from the immutable linux/amd64 Node
24.18.0 Alpine digest. `npm ci` is used in build and production dependency
stages. Runtime uses the non-root `node` user, a read-only root, dropped
capabilities, `no-new-privileges`, tmpfs, explicit CPU/memory, port 3000, and
an internal `/login` health check.

Only Caddy publishes 80/443:

- `/api/*` and `/api/files/*` → `backend:8000`
- `/ui-api/chat` → `frontend:3000` with immediate flush
- other `/ui-api/*`, pages, and assets → `frontend:3000`

SvelteKit emits nonce CSP headers. Caddy preserves HSTS and other security
headers but does not override CSP.

## Verification

```bash
cd backend
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run pytest

cd ../frontend
npm ci
npm run format:check
npm run lint
npm run check
npm test
npm run build
npm run test:e2e
npm audit --audit-level=high

cd ..
python3 scripts/verify_compose_boundary.py
docker compose --env-file deploy/.env -f deploy/compose.yml config --quiet
```

The deterministic browser server covers administrator chat/feedback/preview,
upload, collection mutation, mobile navigation, and keyboard interaction.

Manual acceptance must additionally prove:

- only Caddy publishes;
- frontend and backend health succeed;
- root/login HTML CSP has a nonce and no unsafe script directive;
- `/api/healthz` remains unchanged;
- 50 MB file limits remain enforced;
- chat is visibly incremental/unbuffered;
- Preview performs no `/api/files/*` request/download;
- Preview closes by button, Escape, and backdrop and restores focus.

## Atomic cutover

1. Record the pre-cutover Git SHA.
2. Retain/tag the last known-working `rag-chatbot-streamlit:0.1.0` image.
3. Create `frontend_session_secret`.
4. Build and start `frontend` without changing Caddy.
5. Validate internal health.
6. Recreate Caddy to switch page and `/ui-api` traffic.
7. Run login, chat, upload, preview, CSP, and health smokes.
8. Remove the orphaned Streamlit container.

## Rollback

Restore the recorded Git revision/Caddy/Compose configuration and recreate the
retained Streamlit service, then recreate Caddy and run smoke checks. Do not
downgrade or restore the database: P12 changes no schema. Users sign in again
after rollback.
