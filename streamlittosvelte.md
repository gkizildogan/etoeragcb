# Streamlit-to-SvelteKit Migration Plan

## Summary

- Before implementation, save this plan as `streamlittosvelte.md` in the repository root.
- Replace `streamlit_app/` with a TypeScript SvelteKit application under `frontend/`.
- Preserve FastAPI, its bearer-token API, PostgreSQL, Redis, Qdrant, workers, and model services. The only backend feature addition is an authorized citation-text preview endpoint.
- Deploy SvelteKit as a hardened Docker service using `adapter-node`; Caddy remains the only published service.
- Deliver modern, responsive feature parity with Streamlit, except document citations use an in-app text preview instead of Prepare/Open/download.
- Use a validated atomic cutover. Existing sessions will not migrate; users sign in again after deployment.

## Implementation Changes

### SvelteKit application and authentication

- Baseline on Node 24.18.0 LTS, Svelte 5.56.8, SvelteKit 2.70.1, and `adapter-node` 5.5.7. Pin direct dependencies exactly, commit `package-lock.json`, use `npm ci`, and pin the Node image by full digest. SvelteKit officially supports a standalone Node server built with `adapter-node`, and requires a correct `ORIGIN` behind a proxy. ([SvelteKit Node deployment](https://svelte.dev/docs/kit/adapter-node), [Node release status](https://nodejs.org/en/about/previous-releases))
- Use strict TypeScript, Svelte 5 conventions, vanilla component-scoped CSS/design tokens, ESLint, Prettier, `svelte-check`, Vitest, Testing Library, and Playwright.
- Implement `/login`, `/chat`, `/documents`, and `/collections` with a responsive desktop sidebar/mobile drawer.
- Preserve login/logout, password-based tenant switching, role-aware controls, session CRUD, scoped chat, web-search selection, SSE streaming/replay, feedback, upload/version polling, reindex/delete, and collection management.
- Use SvelteKit as a gateway: page actions, server loads, and `/ui-api/*` endpoints call FastAPI over `http://backend:8000/api`; browser code never receives bearer or refresh tokens.
- Store the versioned token bundle in an AES-256-GCM-encrypted `__Host-rag_session` cookie with `Secure`, `HttpOnly`, `SameSite=Lax`, and `Path=/`. Load a separate 32-byte key from `frontend_session_secret`.
- Retry once after a FastAPI `401`, rotate the cookie after refresh, and clear it after hard expiry/logout. Deduplicate concurrent refreshes and retain a short, bounded mapping from an old refresh-token hash to its rotated result to avoid triggering reuse detection.
- Use runtime response validation, bounded request/stream timeouts, stable error mapping, and no logging of passwords, tokens, uploaded content, or preview text.
- Render Markdown with raw HTML disabled and only safe link protocols. Keep SSE `replace` authoritative, tolerate unknown events, and reconnect once with the same request body, request UUID, and idempotency key.

### Citation preview API and UI

Add:

```text
GET /api/messages/{message_id}/citations/{source_id}/preview
```

Return:

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

- Accept only document citations belonging to an assistant message owned by the active user and tenant.
- Resolve `source_id` through the message’s existing citation and retrieval metadata, then load the matching PostgreSQL `Chunk`.
- Cross-check tenant, document, version, and chunk identifiers; require a non-deleted document and an active/ready/superseded version.
- Return generic `404` for malformed, cross-user, cross-tenant, deleted, garbage-collected, or mismatched sources. Set `Cache-Control: private, no-store`.
- Require no database migration; existing messages already retain the candidate ID needed for best-effort previews.
- Keep the signed-file endpoints for API compatibility, but remove their use from the SvelteKit interface.
- Display document name and page range with a `Preview` button. Open a scrollable, plain-text `<dialog>` modal and close it through its button, Escape, or backdrop click/tap. Restore focus after closing.
- Keep web citations as validated external links with `noopener noreferrer`; do not persist or preview web excerpts.
- If an older source has been deleted or garbage-collected, show “Preview is no longer available” without exposing backend details.

### Containers, ingress, and reproducibility

- Add a multi-stage `frontend/Dockerfile`: build with the digest-pinned Node image, install with `npm ci`, run as a non-root user, expose port 3000, and start with `node build`.
- Replace the Compose `streamlit` service/image with `frontend` / `rag-chatbot-frontend:0.1.0`. Keep it only on the internal `edge` network with a read-only root, dropped capabilities, `no-new-privileges`, tmpfs, resource limits, and an internal health check.
- Configure `ORIGIN=https://${PUBLIC_DOMAIN}`, `API_INTERNAL_URL`, `BODY_SIZE_LIMIT=55M`, shutdown timeout, and the new Docker secret.
- Route `/ui-api/*` and all page/assets traffic to `frontend:3000`; keep `/api/*` and `/api/files/*` routed to FastAPI. Disable proxy buffering for streamed chat.
- Move HTML CSP ownership to SvelteKit’s nonce-based CSP and remove Streamlit’s `unsafe-inline`/`unsafe-eval` policy from Caddy. Preserve HSTS and the existing security headers.
- Add the Node base-image digest to `docker-images.lock`; replace Streamlit pip auditing with `npm audit`; update Grype scans to scan the frontend image.
- Run the Python backend and Node frontend checks separately. Frontend checks cover install, formatting, lint, type/Svelte checks, unit tests, production build, Playwright tests, dependency audit, and image scan.

## Replacement and Documentation Inventory

| Action | Repository areas |
|---|---|
| Remove | Entire `streamlit_app/` tree, Python UI locks/tests, and Streamlit-specific Compose/image references |
| Add | `frontend/`, SvelteKit source/tests/locks/Dockerfile, `frontend_session_secret`, citation-preview API schema/route/tests |
| Infrastructure updates | Compose, both Caddyfiles, boundary verification, dependency/security/release scripts, `.gitignore`, image lock |
| Primary documentation | Rewrite `architecture.md` diagrams, repository map, frontend/auth flow, trust boundaries, tests, and development guidance; rewrite `adminworks.md` configuration, secret creation, health/logging, frontend deployment, audit, rollback, and troubleshooting commands |
| Supporting documentation | Update `README.md`, `AGENTS.md`, `docs/deployment.md`, and the release checklist; add `docs/p12-sveltekit.md`; mark `docs/p9-streamlit.md` as historical/superseded rather than deleting its evidence; add a P12 migration entry to the phased plan |

## Test and Cutover Plan

- Backend tests: successful owned preview, historical citation, malformed source ID, cross-user/tenant denial, web-source denial, deleted/GC source, metadata mismatch, no-store headers, and unchanged signed-file behavior.
- Frontend unit/component tests: encrypted-cookie tamper/expiry, refresh rotation and concurrency, logout/tenant reset, API validation, every SSE event, fragmented streams, replay, citation filtering, modal keyboard/focus behavior, role-based controls, polling, uploads, empty/error states, and responsive navigation.
- Playwright flows: administrator and member parity, chat streaming and feedback, document ingestion polling, collection changes, tenant switching, mobile layout, and accessibility checks. Use a deterministic mock FastAPI server for browser automation.
- Deployment acceptance: frontend image is healthy; only Caddy publishes 80/443; root HTML has a nonce CSP without unsafe script directives; `/api/healthz` is unchanged; uploads respect the 50 MB limit; chat streams without buffering.
- Manual citation acceptance: clicking `Preview` fetches only JSON chunk text, opens the modal at the cited document/page, performs no `/api/files/*` request or download, and closes by button, Escape, or backdrop.
- Atomic cutover: record the pre-cutover Git SHA and retain/tag the working Streamlit image; build and start the new frontend internally; validate health; recreate Caddy to switch traffic; run login/chat/upload/preview smokes; then remove the orphaned Streamlit container.
- Rollback: restore the recorded Git revision/Caddy configuration and recreate the retained Streamlit service. No database rollback is required because the preview endpoint introduces no schema change.
