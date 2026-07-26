# ETOERAGCB administrator runbook

This runbook covers configuration, deployment, user administration, health,
logs, audit, backup, release, SvelteKit cutover, rollback, and troubleshooting.
Commands assume the repository root unless stated otherwise.

## Safety rules

- Never commit `deploy/.env`, `deploy/secrets/*`, documents, model caches,
  backups, audit artifacts, browser traces, or generated builds.
- Caddy is the only service allowed to publish host ports.
- Use Compose service names for internal diagnostics; do not publish a
  temporary database/model port.
- Do not paste authentication, uploaded content, prompts, preview text, signed
  URLs, or secret-bearing configuration into logs/tickets.
- Record a Git SHA before every production cutover.

Define the normal command once in an interactive shell if useful:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml ps
```

## Host prerequisites

- Linux, Docker Engine, and Compose v2
- NVIDIA driver/container toolkit matching the qualified P0 host
- pinned Hugging Face snapshots under `model-cache/huggingface/hub`
- Python 3.13 and `uv` for local backend checks
- Node 24.18.0 and npm for local frontend checks
- an authenticated management path separate from the application

The application stack publishes TCP 80/443 only. For public mode, DNS and
inbound routing must point to Caddy. For LAN mode, use the mDNS hostname and
trust only the Caddy root CA exported from this deployment.

## Configuration

Copy one template:

```bash
cp deploy/.env.example deploy/.env
# or:
cp deploy/.env.lan.example deploy/.env
```

Set `PUBLIC_DOMAIN`, TLS mode/Caddyfile, backup destination, SearXNG secret,
model-cache path, secret GID, retention, and maintenance policy. Do not put
secret values in `.env`; it contains only secret file locations.

Create every file in `deploy/secrets/README.md` with owner/group mode `0640`.
The frontend requires an independent, exactly 32-byte AES key:

```bash
openssl rand -base64 32 > deploy/secrets/frontend_session_secret
chmod 0640 deploy/secrets/frontend_session_secret
```

Do not reuse `jwt_secret` or `signing_secret`. Losing or rotating the frontend
key signs every browser out but does not damage backend data. Users must sign
in again.

Frontend runtime settings are resolved in Compose:

```text
ORIGIN=https://${PUBLIC_DOMAIN}
API_INTERNAL_URL=http://backend:8000/api
BODY_SIZE_LIMIT=55M
SHUTDOWN_TIMEOUT=30
FRONTEND_SESSION_SECRET_FILE=/run/secrets/frontend_session_secret
```

`ORIGIN` must match the browser-visible origin. A mismatch can break form-origin
validation or generated redirects.

## Validation and first start

```bash
python3 scripts/verify_compose_boundary.py
docker compose --env-file deploy/.env -f deploy/compose.yml config --quiet
docker compose --env-file deploy/.env -f deploy/compose.yml up -d --build
docker compose --env-file deploy/.env -f deploy/compose.yml ps
```

Expected healthy services include `caddy`, `frontend`, `backend`, PostgreSQL,
Redis, and `web-fetcher`; model services may have longer startup. `migrate` and
`storage-init` exit successfully.

Inspect structured logs:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml logs \
  --since 15m caddy frontend backend worker
```

Tokens, passwords, uploaded content, raw prompts, and citation preview text must
not appear. The frontend intentionally uses generic errors and does not log
request bodies.

## HTTPS and frontend acceptance

Public checks:

```bash
curl -I "http://${PUBLIC_DOMAIN}/"
curl -fsS "https://${PUBLIC_DOMAIN}/api/healthz"
curl -I "https://${PUBLIC_DOMAIN}/api/readyz"
curl -I "https://${PUBLIC_DOMAIN}/api/metrics"
curl -sS -D - -o /dev/null "https://${PUBLIC_DOMAIN}/login"
```

Requirements:

- HTTP redirects to HTTPS.
- `/api/healthz` returns `{"status":"ok"}`.
- public `/api/readyz` and `/api/metrics` return 404.
- HSTS, `nosniff`, frame, referrer, and permissions headers remain.
- Login HTML has a CSP nonce and no `script-src 'unsafe-inline'` or
  `script-src 'unsafe-eval'`.
- only Caddy publishes 80/443.

Internal frontend health:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml exec -T frontend \
  node -e "fetch('http://127.0.0.1:3000/login').then(r=>{console.log(r.status);if(!r.ok)process.exit(1)})"
```

Manual user acceptance:

1. Sign in as an administrator and a member.
2. Create/delete a private session and stream a response.
3. Enable web search and verify document evidence still remains available.
4. Save positive and negative feedback.
5. Upload a supported file and follow version polling.
6. Reindex/delete as administrator; confirm role controls are absent for a
   member.
7. Create/edit/delete a collection and change document membership.
8. Switch tenant with password confirmation when multiple memberships exist.
9. At desktop and mobile widths, verify sidebar/drawer navigation.

Citation acceptance:

1. Click a document `Preview`.
2. Confirm the browser requests only
   `/ui-api/messages/<message>/citations/<source>/preview`.
3. Confirm no `/api/files/*` request or download occurs.
4. Confirm exact plain text and document/page metadata appear.
5. Close by the button, Escape, and backdrop; focus returns to `Preview`.
6. For deleted/collected evidence, verify only “Preview is no longer
   available.”
7. Confirm web citations open credential-free HTTP(S) URLs in a new context.

## LAN internal CA

Export the local CA only from this Caddy instance:

```bash
mkdir -p artifacts/p1
docker compose --env-file deploy/.env -f deploy/compose.yml cp \
  caddy:/data/caddy/pki/authorities/local/root.crt \
  artifacts/p1/caddy-local-root.crt
```

Install it as a user-trusted CA on each test device and restart the browser.
Browse the hostname from `PUBLIC_DOMAIN`, never a raw DHCP address. Keep devices
off guest/client-isolated Wi-Fi.

## Closed user administration

Bootstrap once:

```bash
python3 scripts/seed_admin.py \
  --email admin@example.com \
  --tenant-slug shared \
  --tenant-name "Shared Knowledge"
```

Create a later user:

```bash
python3 scripts/create_user.py \
  --actor-email admin@example.com \
  --email member@example.com \
  --tenant-slug shared \
  --role member
```

Common account controls:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml exec backend \
  python -m app.admin_cli disable-user \
  --actor-email admin@example.com --email member@example.com

docker compose --env-file deploy/.env -f deploy/compose.yml exec backend \
  python -m app.admin_cli reset-password \
  --actor-email admin@example.com --email member@example.com

docker compose --env-file deploy/.env -f deploy/compose.yml exec backend \
  python -m app.admin_cli set-role \
  --actor-email admin@example.com --email member@example.com \
  --tenant-slug shared --role admin

docker compose --env-file deploy/.env -f deploy/compose.yml exec backend \
  python -m app.admin_cli revoke-tokens \
  --actor-email admin@example.com --email member@example.com
```

Each command verifies the acting superuser. Disable, password reset, role
change, and explicit revocation invalidate sessions.

## Development verification

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
npx playwright install chromium
npm run test:e2e
npm audit --audit-level=high
```

The Playwright suite starts a deterministic mock FastAPI service. It does not
need PostgreSQL, Redis, Qdrant, models, secrets, or document storage.

## Dependency and image audit

```bash
deploy/dependency-audit.sh
deploy/security-scan.sh
```

`dependency-audit.sh` runs hash-locked backend `pip-audit` and frontend
`npm audit --audit-level=high`. `security-scan.sh` builds/scans
`rag-chatbot-backend:0.1.0` and `rag-chatbot-frontend:0.1.0` with the pinned
Grype image. Evidence is written under ignored `artifacts/p11/security/`.

Do not run automatic audit fixes. Update exact direct pins deliberately,
regenerate `package-lock.json`, review transitive changes, rerun the full
frontend pipeline, and rebuild/scan the image.

## Backup, restore, and retention

P12 adds no database migration. Existing P11 backup and restore mechanics are
unchanged. Backups cover PostgreSQL, Qdrant, document files, configuration
evidence, and encrypted off-machine transfer; browser cookies are intentionally
not backed up.

Use:

```bash
deploy/backup.sh
deploy/restore-drill.sh
deploy/failure-drills.sh
```

The off-machine destination must be authenticated and restricted. Restic
encrypts before rclone transfer. Keep the Restic password and authenticated
rclone configuration in separate offline recovery storage. Retention remains
blocked unless the configured recent verified backup gate passes.

## Monitoring and private diagnostics

Public Caddy returns 404 for readiness/metrics. Query inside:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml exec backend \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/readyz').read().decode())"
```

Prometheus and Alertmanager remain optional profiles on private networks. See
`docs/p11-operations.md` and `docs/incident-runbooks.md` for alert ownership and
failure response.

## Atomic SvelteKit cutover

Before cutover:

```bash
git rev-parse HEAD
docker image inspect rag-chatbot-streamlit:0.1.0
docker tag rag-chatbot-streamlit:0.1.0 \
  rag-chatbot-streamlit:pre-p12-$(date -u +%Y%m%dT%H%M%SZ)
```

Retain the printed Git SHA and image tag in the change record. Then:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml build frontend
docker compose --env-file deploy/.env -f deploy/compose.yml up -d frontend
docker compose --env-file deploy/.env -f deploy/compose.yml ps frontend
docker compose --env-file deploy/.env -f deploy/compose.yml up -d \
  --no-deps --force-recreate caddy
```

Run login/chat/upload/preview/CSP smokes. After acceptance, remove only the
orphaned old container:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml up -d --remove-orphans
```

This cutover does not migrate browser sessions. Users sign in again.

## Rollback

Rollback is application/configuration-only:

1. Restore the recorded pre-cutover Git revision in a clean worktree or release
   checkout.
2. Restore the old Caddy/Compose configuration.
3. Recreate the retained Streamlit service and then Caddy.
4. Run HTTPS login/chat health smokes.

No Alembic downgrade or data restore is required because the citation preview
endpoint adds no table/column/index.

Do not delete current data volumes or run a database downgrade during P12
rollback.

## Troubleshooting

### Browser returns to login immediately

- Verify `frontend_session_secret` exists, is readable through `SECRETS_GID`,
  decodes to exactly 32 bytes, and did not change between replicas/restarts.
- Check `ORIGIN` exactly matches the HTTPS domain.
- Verify browser access is HTTPS; `__Host-rag_session` is always Secure.
- Check backend auth logs by request ID, never by tokens.

### Repeated 401 or refresh reuse detection

- Confirm only one frontend deployment key is in use.
- Ensure all frontend replicas run the same code/key.
- Do not add independent client-side token refresh.
- Reproduce with one request; concurrent refreshes should be deduplicated and
  old-token rotation results retained briefly.
- Clear the cookie/sign in again after a confirmed expired/revoked session.

### Chat stops or buffers

- Verify both Caddy `/ui-api/chat` and backend `/api/*` proxies use
  `flush_interval -1`.
- Confirm the browser retries at most once with the identical body,
  `client_request_id`, and `Idempotency-Key`.
- Inspect backend request ID and idempotent replay headers.
- Check vLLM/readiness and generation timeout without logging prompt content.

### Upload rejected

- Keep files below 50 MB; Caddy and FastAPI limits differ intentionally by
  overhead (`55 MB` versus `50 MB` file payload).
- Use PDF, TXT, Markdown, JSONL, or DOCX.
- Check quota, MIME validation, administrator role, and ingestion queue health.

### Citation preview unavailable

- A 404 is intentionally generic. Confirm the message belongs to the active
  user/tenant and the source is a document citation.
- Check whether its document was deleted or version garbage-collected.
- For new messages, inspect stored metadata internally for matching citation,
  candidate UUID, document/version, content hash, section, and chunk index.
- Never return web excerpt text or a database mismatch detail to the browser.

### CSP failure

- Inspect the HTML response from SvelteKit, not an API response.
- Confirm a nonce is present and unsafe script directives are absent.
- Confirm Caddy does not set/overwrite `Content-Security-Policy`.
- Rebuild frontend after CSP configuration changes.

### Frontend container unhealthy

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml logs --tail 200 frontend
docker compose --env-file deploy/.env -f deploy/compose.yml exec frontend \
  node -e "fetch('http://127.0.0.1:3000/login').then(async r=>console.log(r.status,await r.text()))"
```

Check secret readability, Node image pin, production dependencies, port 3000,
and adapter-node startup. Do not temporarily publish port 3000.
