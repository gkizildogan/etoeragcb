# ETOERAGCB administrator runbook

This runbook covers first deployment, routine operation, users and tenants,
application data, PostgreSQL, Qdrant, Redis, model services, monitoring,
backup/restore, upgrades, and resets.

Run commands from the repository root unless a section says otherwise. Replace
all example hostnames, emails, tenant slugs, UUIDs, and backup destinations.
Never paste passwords, tokens, signed URLs, private document text, or secret
file contents into logs or tickets.

## Contents

1. Compose file selection
2. Configuration files
3. First deployment
4. Routine lifecycle
5. Status, health, logs, and consistency
6. Users and tenants
7. Collections, documents, chats, and feedback
8. PostgreSQL
9. Qdrant
10. Redis
11. Raw document storage
12. Model and retrieval services
13. Backup, restore, and retention
14. Monitoring and alerts
15. Deploying changes
16. Reset procedures
17. Incident quick reference
18. Post-change checklist

## 1. Know which Compose file to use

| File | Use |
|---|---|
| `deploy/compose.yml` | The normal application stack. Use this for deployment and daily administration. |
| `deploy/restore-compose.yml` | An isolated clean-volume restore drill created by `deploy/restore-drill.sh`. Never use it as the normal application deployment. |
| `p0/compose.yml` | GPU/model qualification before application deployment. It does not run the application, database, or UI. |

`deploy/compose.yml` also defines two opt-in profiles:

- `monitoring`: long-running Prometheus and Alertmanager services;
- `operations`: one-shot Restic, rclone, dump, and backup helper containers.

Do not start the `operations` profile as a long-running stack. Use the checked-in
backup/restore scripts, which invoke those helpers with the required staging
paths and cleanup.

The normal command prefix is:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml
```

## 2. Configuration file map

| Change | File(s) to edit |
|---|---|
| LAN hostname, TLS mode, model-cache host path, backup destination/retention, secret paths | `deploy/.env` copied from `.env.lan.example` or `.env.example` |
| Services, images, CPU/RAM/GPU limits, ports, networks, RAG limits, model IDs, cache TTLs | `deploy/compose.yml` |
| Public ACME routing/TLS/security headers | `deploy/Caddyfile` |
| LAN internal-CA routing/TLS/security headers | `deploy/Caddyfile.lan` |
| SearXNG engines/formats/safety | `deploy/searxng/settings.yml` |
| Prometheus targets/rules | `deploy/monitoring/prometheus.yml`, `alerts.yml` |
| Alert email receiver | generated `deploy/state/alertmanager/alertmanager.yml` |
| Backend build/dependencies | `backend/Dockerfile`, `backend/pyproject.toml`, lock files |
| Streamlit build/dependencies | `streamlit_app/Dockerfile`, `streamlit_app/pyproject.toml`, lock files |
| Database schema | `backend/app/models/` plus a new file under `backend/alembic/versions/` |
| Immutable model revisions/cache paths | `model-revisions.lock` and matching Compose values |
| Third-party image pins | `docker-images.lock` and matching Compose image references |

`deploy/.env`, `deploy/secrets/`, `deploy/state/`, model files, document fixtures,
and operational artifacts are intentionally ignored by Git.

After every Compose or environment edit, validate before applying:

```bash
python3 scripts/verify_compose_boundary.py
docker compose --env-file deploy/.env -f deploy/compose.yml \
  --profile monitoring --profile operations config --quiet
```

## 3. First deployment from a fresh clone

Clone into the final intended path because the optional systemd backup unit
contains a repository path:

```bash
git clone REPOSITORY_URL etoeragcb
cd etoeragcb
```

Replace `REPOSITORY_URL` with the real Git remote. A Git clone intentionally
does not include model snapshots, secrets, runtime state, backups, or local
artifacts; provision them in the following steps.

### 3.1 Host prerequisites

The qualified target is a Linux host with:

- Docker Engine and Docker Compose v2;
- NVIDIA driver, NVIDIA Container Toolkit, and a Docker-visible GPU;
- the hardware capacity accepted in `docs/feasibility.md` (the current
  qualification used an RTX 3090 and 64 GB host RAM);
- Python 3 and OpenSSL for repository helper scripts;
- enough disk for model snapshots, Docker images/volumes, documents, backups,
  and restore drills.

Check the host:

```bash
docker version
docker compose version
nvidia-smi
```

### 3.2 Provision the model cache

Models are not stored in Git. Copy a verified Hugging Face cache from another
qualified host or download the exact revisions named in
`model-revisions.lock`. The required snapshot directories are:

```text
model-cache/huggingface/hub/
  models--cyankiwi--Qwen3.5-9B-AWQ-4bit/
    snapshots/73536aa464f9a93c550aa5a916f0113a08b2f384/
  models--BAAI--bge-m3/
    snapshots/5617a9f61b028005a4858fdac845db406aefb181/
  models--BAAI--bge-reranker-v2-m3/
    snapshots/953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e/
```

Hugging Face snapshot files are normally symlinks into each repository's
sibling `blobs/` directory. Copy each complete `models--...` directory,
including `blobs`, `refs`, and `snapshots`; copying only the snapshot symlinks
creates a broken cache. Do not replace commit directories with a moving `main`
snapshot.

If the Hugging Face `hf` CLI is installed, these commands populate the expected
cache layout with the exact revisions:

```bash
HF_HOME="$PWD/model-cache/huggingface" hf download \
  cyankiwi/Qwen3.5-9B-AWQ-4bit \
  --revision 73536aa464f9a93c550aa5a916f0113a08b2f384
HF_HOME="$PWD/model-cache/huggingface" hf download \
  BAAI/bge-m3 \
  --revision 5617a9f61b028005a4858fdac845db406aefb181
HF_HOME="$PWD/model-cache/huggingface" hf download \
  BAAI/bge-reranker-v2-m3 \
  --revision 953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e
```

Pull the three P0 images named in the lock file:

```bash
set -a
. ./docker-images.lock
set +a
docker pull "$VLLM_IMAGE"
docker pull "$TEI_IMAGE"
docker pull "$PYTHON_BASE_IMAGE"
unset VLLM_IMAGE TEI_IMAGE PYTHON_BASE_IMAGE
```

Then verify the cache and image pins:

```bash
./scripts/p0/run.sh verify
```

On an unqualified host, run the complete P0 sequence in `p0/README.md` before
trusting the application stack.

### 3.3 Create the deployment environment

For same-Wi-Fi/LAN deployment:

```bash
cp deploy/.env.lan.example deploy/.env
```

For a real public DNS name and ACME:

```bash
cp deploy/.env.example deploy/.env
```

Edit `deploy/.env`:

- set `PUBLIC_DOMAIN` to the LAN mDNS name or public DNS name;
- use `TLS_MODE=internal` and `CADDYFILE=./Caddyfile.lan` for LAN;
- use `TLS_MODE=public`, `CADDYFILE=./Caddyfile`, and a real `ACME_EMAIL` for
  public deployment;
- set `MODEL_CACHE_HOST_PATH`;
- set `SECRETS_GID` to the deployment operator's primary group from `id -g`;
- replace the example backup folder with an operator-owned restricted
  destination;
- replace the SearXNG placeholder with a random value of at least 32
  characters.

For LAN mode, keep clients on the same non-guest network and ensure the mDNS
name resolves to the host. For public mode, create the DNS `A`/`AAAA` records,
route TCP 80/443 to this host, and retain a separately restricted SSH/VPN
administration path. No backend, data, model, search, or monitoring port should
be published.

Generate a SearXNG secret without printing it into a shared terminal:

```bash
openssl rand -hex 32
```

Paste the result only into the ignored `deploy/.env`.

### 3.4 Create deployment secrets

Create strong, different values. The hexadecimal PostgreSQL password below is
URL-safe, so the database DSN can use it without additional encoding.

```bash
install -d -m 0750 deploy/secrets
umask 0027

openssl rand -hex 32 > deploy/secrets/postgres_password
openssl rand -hex 32 > deploy/secrets/jwt_secret
openssl rand -hex 32 > deploy/secrets/signing_secret
openssl rand -hex 48 > deploy/secrets/backup_encryption_key

db_password="$(<deploy/secrets/postgres_password)"
printf 'postgresql+asyncpg://rag:%s@postgres:5432/rag\n' \
  "$db_password" > deploy/secrets/database_url
unset db_password

chmod 0640 \
  deploy/secrets/postgres_password \
  deploy/secrets/database_url \
  deploy/secrets/jwt_secret \
  deploy/secrets/signing_secret \
  deploy/secrets/backup_encryption_key
```

The operator/group configured by `SECRETS_GID` must be able to read these
files. Keep offline recovery copies of all secrets, especially the Restic
password. JWT and signed-file HMAC secrets must be different.

SMTP is optional until the monitoring profile is enabled. Configure it through
the hidden prompt described under Monitoring; do not create a fake production
SMTP password.

### 3.5 Validate and start

```bash
python3 scripts/verify_compose_boundary.py
docker compose --env-file deploy/.env -f deploy/compose.yml config --quiet
docker compose --env-file deploy/.env -f deploy/compose.yml up -d --build
docker compose --env-file deploy/.env -f deploy/compose.yml ps
```

The one-shot `storage-init` and `migrate` services should exit successfully.
The long-running services should be running, and services with health checks
should become healthy.

Check internal readiness:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml exec -T backend \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/readyz').read().decode())"
```

Check HTTPS from the host:

```bash
deployment_domain="$(python3 scripts/env_value.py deploy/.env PUBLIC_DOMAIN)"
curl -I "http://${deployment_domain}/"
curl -kfsS "https://${deployment_domain}/api/healthz"
unset deployment_domain
```

`-k` is acceptable only for this initial LAN probe before the local CA is
trusted. Do not use it for routine verification.

### 3.6 Trust the LAN CA

For LAN mode, export Caddy's local root:

```bash
mkdir -p artifacts/p1
docker compose --env-file deploy/.env -f deploy/compose.yml cp \
  caddy:/data/caddy/pki/authorities/local/root.crt \
  artifacts/p1/caddy-local-root.crt
```

Install that exact certificate as a trusted user CA on each test device, restart
the browser, and open `https://PUBLIC_DOMAIN`. If the Caddy data volume is
deleted, a new CA is generated and every client must trust the replacement.

### 3.7 Bootstrap the first administrator

There is no public registration endpoint:

```bash
python3 scripts/seed_admin.py \
  --email admin@example.com \
  --tenant-slug shared \
  --tenant-name "Shared Knowledge"
```

Passwords are prompted twice and do not enter shell history. Bootstrap refuses
to run after any superuser exists.

### 3.8 Optional monitoring and off-machine backup

Monitoring:

```bash
ALERT_EMAIL=operator@example.com deploy/configure-alert-email.sh
docker compose --env-file deploy/.env -f deploy/compose.yml \
  --profile monitoring up -d alertmanager prometheus
```

Google Drive backup:

```bash
deploy/configure-gdrive-backup.sh
deploy/backup.sh
deploy/restore-drill.sh
```

The Drive folder must be Restricted. Restic encryption happens before rclone
uploads any repository objects.

## 4. Routine lifecycle

Start or converge the normal stack:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml up -d
```

Start including monitoring:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml \
  --profile monitoring up -d
```

Stop containers without deleting them or their data:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml \
  --profile monitoring stop
```

Restart one service:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml restart worker
```

Remove containers and networks while preserving named volumes:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml \
  --profile monitoring down
```

Never add `--volumes` casually. It deletes persistent stores.

## 5. Status, health, logs, and consistency

Container status:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml ps --all
docker compose --env-file deploy/.env -f deploy/compose.yml top
docker stats --no-stream
nvidia-smi
```

Recent logs:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml \
  logs --since=30m --tail=300 backend worker
docker compose --env-file deploy/.env -f deploy/compose.yml \
  logs --since=30m --tail=200 postgres qdrant redis
docker compose --env-file deploy/.env -f deploy/compose.yml \
  logs --since=30m --tail=200 vllm tei-embed tei-rerank
```

Do not export unrestricted model/application logs when real user data is
present. Current code avoids logging prompts and retrieved content, but review
before sharing.

Public liveness and private readiness:

```bash
deployment_domain="$(python3 scripts/env_value.py deploy/.env PUBLIC_DOMAIN)"
curl --cacert artifacts/p1/caddy-local-root.crt \
  "https://${deployment_domain}/api/healthz"
unset deployment_domain

docker compose --env-file deploy/.env -f deploy/compose.yml exec -T backend \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/readyz').read().decode())"
```

Live cross-store verification:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml exec -T backend \
  python -m app.operations.backup verify-live
```

Private metrics:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml exec -T backend \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/metrics').read().decode())"
```

## 6. User and tenant administration

### 6.1 Understand the privilege levels

- A superuser is a global operator identified by `users.is_superuser`.
- `admin` and `member` are roles on one `user_tenants` membership.
- The internal admin CLI requires an active superuser and prompts for that
  acting superuser's password.
- Role/password/enablement changes invalidate the target user's sessions.

### 6.2 Create a normal user

```bash
python3 scripts/create_user.py \
  --actor-email admin@example.com \
  --email member@example.com \
  --tenant-slug shared \
  --role member
```

Create a tenant administrator by using `--role admin`. Create another global
superuser only with explicit review:

```bash
python3 scripts/create_user.py \
  --actor-email admin@example.com \
  --email second-operator@example.com \
  --tenant-slug shared \
  --role admin \
  --superuser
```

### 6.3 Disable, enable, reset, revoke, and change roles

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml exec backend \
  python -m app.admin_cli disable-user \
  --actor-email admin@example.com --email member@example.com

docker compose --env-file deploy/.env -f deploy/compose.yml exec backend \
  python -m app.admin_cli enable-user \
  --actor-email admin@example.com --email member@example.com

docker compose --env-file deploy/.env -f deploy/compose.yml exec backend \
  python -m app.admin_cli reset-password \
  --actor-email admin@example.com --email member@example.com

docker compose --env-file deploy/.env -f deploy/compose.yml exec backend \
  python -m app.admin_cli revoke-tokens \
  --actor-email admin@example.com --email member@example.com

docker compose --env-file deploy/.env -f deploy/compose.yml exec backend \
  python -m app.admin_cli set-role \
  --actor-email admin@example.com --email member@example.com \
  --tenant-slug shared --role admin
```

`set-role` also creates a missing membership for an existing user.

Account deletion is intentionally unsupported. Disable the user and preserve
referential/audit history.

### 6.4 Open an authenticated PostgreSQL console

Use this only from an authorized host operator account:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml exec postgres \
  sh -eu -c 'export PGPASSWORD="$(cat /run/secrets/postgres_password)"; exec psql --username=rag --dbname=rag'
```

In `psql`, enable fail-fast behavior:

```sql
\set ON_ERROR_STOP on
```

Inspect users and memberships without selecting password/token hashes:

```sql
SELECT
    u.email,
    u.is_active,
    u.is_superuser,
    u.auth_version,
    t.slug AS tenant,
    ut.role
FROM users AS u
LEFT JOIN user_tenants AS ut ON ut.user_id = u.id
LEFT JOIN tenants AS t ON t.id = ut.tenant_id
ORDER BY u.email, t.slug;
```

### 6.5 Create or rename a tenant

The current code has no first-class tenant-create API/CLI after bootstrap.
Create the row transactionally, then use `create-user` or `set-role` to add
memberships:

```sql
BEGIN;
INSERT INTO tenants (id, slug, name)
VALUES (gen_random_uuid(), 'research', 'Research Knowledge')
RETURNING id, slug, name, retrieval_revision;
COMMIT;
```

Tenant slugs should use lowercase letters, numbers, and hyphens. Rename the
display name:

```sql
BEGIN;
UPDATE tenants
SET name = 'Research and Engineering'
WHERE slug = 'research'
RETURNING id, slug, name;
COMMIT;
```

Changing a slug affects operator commands and human references. Confirm no
external automation depends on it before:

```sql
BEGIN;
UPDATE tenants
SET slug = 'research-engineering'
WHERE slug = 'research'
RETURNING id, slug, name;
COMMIT;
```

Tenants currently have no active/disabled status column. Do not simulate
tenant suspension by editing unrelated generation fields. If tenant
suspension is required, implement and migrate a first-class status checked by
authentication and every request dependency.

Deleting a tenant is not a routine operation. Foreign keys intentionally
protect active generations and owned content. For production, preserve the
tenant or implement a reviewed deletion workflow. For a disposable environment,
use the complete application-data reset instead of hand-deleting a populated
tenant.

## 7. Collections, documents, chats, and feedback

Use the Streamlit UI or authenticated API for routine CRUD so authorization,
idempotency, retrieval revisions, tombstone generations, and cleanup semantics
are preserved.

| Resource | Normal administration |
|---|---|
| Collections | Members list; tenant admins create, rename, describe, delete, and change document membership |
| Documents | Members inspect; tenant admins upload, create versions, reindex, assign collections, and delete |
| Chat sessions | Each user creates/lists/soft-deletes only their own sessions |
| Messages | Created by the chat transaction; do not edit manually |
| Feedback | User creates/updates a `-1` or `1` rating on an owned assistant message |

Document deletion activates a new manifest excluding the document. It does not
immediately erase raw files or vectors. Backup-gated retention performs safe
physical garbage collection later.

Read-only document/job diagnosis in PostgreSQL:

```sql
SELECT
    d.title,
    d.source_filename,
    d.deleted_at,
    v.version,
    v.status AS version_status,
    v.page_count,
    v.section_count,
    v.chunk_count,
    v.error_code,
    j.status AS job_status,
    j.attempt,
    j.heartbeat_at
FROM documents AS d
JOIN document_versions AS v ON v.document_id = d.id
JOIN ingestion_jobs AS j ON j.document_version_id = v.id
ORDER BY d.created_at DESC, v.version DESC;
```

Do not manually set a version to `active`, a generation to `active`, or a job
to `succeeded`. Those fields must change together in the ingestion activation
transaction.

### Retry a failed ingestion after fixing its cause

The normal choice is to upload the file again. To reuse an existing failed job
whose raw file is still present, first copy its UUID from the diagnostic query,
confirm it is `failed`, then run:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml exec -T worker \
  python - JOB_UUID <<'PY'
import asyncio
import sys

from app.workers import ingest_document, shutdown, startup


async def main() -> None:
    context = {}
    await startup(context)
    try:
        await ingest_document(context, sys.argv[1])
    finally:
        await shutdown(context)


asyncio.run(main())
PY
```

Replace `JOB_UUID` with the real UUID. The pipeline increments the attempt,
clears bounded errors, removes only that inactive version's partial points, and
revalidates before activation.

## 8. PostgreSQL administration

### Routine inspection

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml exec -T postgres \
  pg_isready --username=rag --dbname=rag

docker compose --env-file deploy/.env -f deploy/compose.yml exec -T backend \
  alembic current

docker compose --env-file deploy/.env -f deploy/compose.yml exec -T backend \
  alembic heads
```

Useful read-only counts:

```sql
SELECT 'tenants' AS object, count(*) FROM tenants
UNION ALL SELECT 'users', count(*) FROM users
UNION ALL SELECT 'documents', count(*) FROM documents
UNION ALL SELECT 'active_versions', count(*) FROM document_versions WHERE status = 'active'
UNION ALL SELECT 'chunks', count(*) FROM chunks
UNION ALL SELECT 'sessions', count(*) FROM chat_sessions WHERE deleted_at IS NULL
UNION ALL SELECT 'messages', count(*) FROM messages;
```

### Schema migrations

For a schema change:

1. update SQLAlchemy models;
2. create and review a new Alembic revision;
3. back up and test a clean restore;
4. build the new backend image;
5. run the migration;
6. recreate backend/worker services;
7. verify readiness and live consistency.

Commands:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml build backend
docker compose --env-file deploy/.env -f deploy/compose.yml run --rm migrate
docker compose --env-file deploy/.env -f deploy/compose.yml \
  up -d --no-deps --force-recreate backend worker web-fetcher
```

Do not run Alembic downgrade against live data unless the exact downgrade has
been rehearsed on a restored copy and data loss is explicitly accepted.

### PostgreSQL is not reset independently

PostgreSQL active manifests must match Qdrant points and raw document files.
Restoring or resetting PostgreSQL alone creates an inconsistent system. Restore
all three stores from one verified backup set, or reset all application data in
a disposable environment.

## 9. Qdrant administration

Qdrant is private on the `data` network and has no host port. Use the backend
container as an internal diagnostic client.

Collection health and vector configuration:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml exec -T backend \
  python - <<'PY'
import json
import httpx

response = httpx.get(
    "http://qdrant:6333/collections/rag_chunks_v1",
    timeout=10,
)
response.raise_for_status()
print(json.dumps(response.json(), indent=2))
PY
```

Count points for one tenant or document version:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml exec -T backend \
  python - TENANT_UUID <<'PY'
import json
import sys
import httpx

body = {
    "filter": {
        "must": [
            {"key": "tenant_id", "match": {"value": sys.argv[1]}},
        ]
    },
    "exact": True,
}
response = httpx.post(
    "http://qdrant:6333/collections/rag_chunks_v1/points/count",
    json=body,
    timeout=30,
)
response.raise_for_status()
print(json.dumps(response.json(), indent=2))
PY
```

Qdrant's routine CRUD must be driven by application ingestion, document
deletion, reindexing, and backup-gated garbage collection. Manually editing
points can bypass stable IDs, active manifests, or tenant filters.

In CRUD terms:

- create the collection/schema by running a normal ingestion; the indexer's
  `prepare` operation creates missing vector and payload indexes;
- read collection metadata/counts with the diagnostic requests above;
- update/upsert points only through document ingestion or reindexing;
- delete document visibility through the document API, which activates a
  tombstone manifest;
- physically delete inactive points through backup-gated maintenance;
- delete the whole collection/volume only with the disposable reset below.

Use the integrated consistency checker after any Qdrant incident:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml exec -T backend \
  python -m app.operations.backup verify-live
```

Qdrant snapshots are already included in `deploy/backup.sh`; do not create a
separate vector-only recovery policy.

### Disposable Qdrant-only reset

This creates deliberate PostgreSQL/Qdrant drift until every active document is
reindexed. It is for development only; restoring one consistent backup is
safer.

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml \
  stop backend worker qdrant
docker compose --env-file deploy/.env -f deploy/compose.yml rm -f qdrant
docker volume inspect rag-chatbot_qdrant_data
docker volume rm rag-chatbot_qdrant_data
docker compose --env-file deploy/.env -f deploy/compose.yml up -d qdrant
docker compose --env-file deploy/.env -f deploy/compose.yml up -d worker backend
```

Then reindex every active document through the admin UI and run
`verify-live`. This reset is not an acceptable shared/production recovery
procedure; use a consistent backup restore there.

## 10. Redis administration

Redis contains queue, cache, and rate-limit state but no authoritative
application records.

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml exec -T redis \
  redis-cli PING
docker compose --env-file deploy/.env -f deploy/compose.yml exec -T redis \
  redis-cli DBSIZE
docker compose --env-file deploy/.env -f deploy/compose.yml exec -T redis \
  redis-cli INFO memory
```

To discard Redis state, first ensure no ingestion is active:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml exec -T redis \
  redis-cli FLUSHDB
docker compose --env-file deploy/.env -f deploy/compose.yml restart worker backend
```

Worker startup reconciliation recovers durable staged/queued/stale jobs from
PostgreSQL. Rate-limit and cache state starts empty.

Do not use `KEYS *` on a large production Redis. Use `SCAN` for bounded
diagnosis.

## 11. Raw document storage

Raw files are stored in the Docker volume `rag-chatbot_document_files`, mounted
at `/data/documents` only in backend/worker/controlled operation containers.
The path hierarchy is tenant/document/version based. Caddy and Streamlit never
mount the volume.

Inspect metadata, not content:

```bash
docker volume inspect rag-chatbot_document_files
docker compose --env-file deploy/.env -f deploy/compose.yml exec -T backend \
  find /data/documents -type f -printf '%s %p\n'
```

Never delete a raw file manually. Database rows and signed-file integrity
checks retain its expected size/hash, and backup manifests include it. Use the
document API plus maintenance, or restore a complete backup set.

## 12. Model and retrieval services

Health:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml exec -T backend \
  python - <<'PY'
import httpx

for name, url in {
    "vllm": "http://vllm:8000/health",
    "embed": "http://tei-embed:8080/health",
    "rerank": "http://tei-rerank:8080/health",
    "qdrant": "http://qdrant:6333/readyz",
}.items():
    response = httpx.get(url, timeout=10)
    print(name, response.status_code)
PY
```

Restart only the failed pinned service:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml restart vllm
```

After a model outage or GPU OOM, inspect `nvidia-smi`, container limits, and
logs. Keep generation concurrency at the P0-qualified value unless a new
qualification proves another limit.

A model/revision/tokenizer/embedding-dimension change requires more than a
Compose edit:

- update pins and cache paths;
- rerun P0;
- rebuild affected application images;
- recreate/reindex Qdrant content when vector/chunk semantics change;
- regenerate and review P10 evaluation/calibration;
- run all live smokes and release checks.

SearXNG and web-fetcher failures should degrade only the optional web branch.
Do not give the backend direct internet access as a workaround.

## 13. Backup, restore, and retention

### Manual backup

Local encrypted rehearsal:

```bash
BACKUP_SKIP_REMOTE=1 deploy/backup.sh
```

Verified off-machine backup:

```bash
deploy/backup.sh
```

The script briefly quiesces backend/worker, dumps PostgreSQL, snapshots Qdrant,
archives raw files, validates identities, restarts traffic, encrypts with
Restic, verifies the repository, uploads with rclone, compares remote content,
and atomically updates the success marker.

### Scheduled backup

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/rag-chatbot-backup.{service,timer} \
  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now rag-chatbot-backup.timer
systemctl --user list-timers rag-chatbot-backup.timer
journalctl --user -u rag-chatbot-backup.service
```

The checked-in unit assumes the repository is at
`%h/projects/etoeragcb`; edit both unit paths if the clone is elsewhere.

### Restore drill

```bash
deploy/restore-drill.sh
```

This downloads/decrypts the repository into a temporary path, restores into a
separate uniquely named Compose project, validates PostgreSQL counts, active
generation manifests, raw-file hashes, Qdrant counts/retrieval, and signed-file
bindings, writes a report under `artifacts/p11/`, and deletes only the isolated
drill resources.

For an emergency production restore, preserve failed volumes, run the clean
drill first, and restore PostgreSQL, Qdrant, and raw files from the same
snapshot. Follow `docs/incident-runbooks.md` and `docs/p11-operations.md`; the
checked script intentionally validates an isolated restore rather than
silently replacing live volumes.

### Retention

Daily maintenance always prunes old ephemeral token/idempotency rows. It only
garbage-collects inactive raw/chunk/vector payload when
`deploy/state/backup/last-success.json` proves a recent encrypted,
repository-checked, authenticated off-machine backup. Never fabricate that
marker or bypass the gate.

## 14. Monitoring and alerts

Prometheus and Alertmanager are private; neither publishes a host port.

Configure Gmail SMTP:

```bash
ALERT_EMAIL=operator@example.com deploy/configure-alert-email.sh
```

Use a dedicated Google app password, not the account password. Start:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml \
  --profile monitoring up -d alertmanager prometheus
```

Send an explicit test:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml \
  --profile monitoring exec -T alertmanager \
  amtool --alertmanager.url=http://127.0.0.1:9093 alert add \
  alertname=EtoeragcbSmtpTest severity=warning \
  --annotation=summary='SMTP delivery test' \
  --annotation=description='Manual test; no application failure occurred.'
```

The application admin email is unrelated to SMTP. Alert delivery configuration
lives in ignored runtime state.

## 15. Deploying code and configuration changes

### Backend-only source change

The backend, worker, and web-fetcher share one image tag:

```bash
cd backend
.venv/bin/ruff check app tests
.venv/bin/mypy app
.venv/bin/pytest
cd ..

docker compose --env-file deploy/.env -f deploy/compose.yml build backend
docker compose --env-file deploy/.env -f deploy/compose.yml \
  up -d --no-deps --force-recreate backend worker web-fetcher
```

If the change includes a migration, run the migration sequence in the
PostgreSQL section before recreating application services.

### Streamlit-only change

```bash
cd streamlit_app
.venv/bin/ruff check .
.venv/bin/pytest
cd ..

docker compose --env-file deploy/.env -f deploy/compose.yml build streamlit
docker compose --env-file deploy/.env -f deploy/compose.yml \
  up -d --no-deps --force-recreate streamlit
```

### Compose/environment change

Validate the rendered configuration and boundary, then converge:

```bash
python3 scripts/verify_compose_boundary.py
docker compose --env-file deploy/.env -f deploy/compose.yml config --quiet
docker compose --env-file deploy/.env -f deploy/compose.yml up -d
```

Environment changes are read only at container creation; use
`--force-recreate` for affected services when in doubt.

### Caddy change

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml exec -T caddy \
  caddy validate --config /etc/caddy/Caddyfile
docker compose --env-file deploy/.env -f deploy/compose.yml \
  up -d --no-deps --force-recreate caddy
```

### Release evidence

```bash
deploy/dependency-audit.sh
deploy/security-scan.sh
deploy/load-smoke.sh health
deploy/release-check.sh
```

Run `deploy/failure-drills.sh` only in an approved window; it deliberately
flushes Redis and stops/restarts core dependencies.

## 16. Reset procedures

> **Destructive:** Every reset below must be preceded by target verification
> and a tested backup unless the environment is explicitly disposable.

### No-data-loss restart

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml \
  --profile monitoring stop
docker compose --env-file deploy/.env -f deploy/compose.yml \
  --profile monitoring up -d
```

### Application-data reset while preserving LAN CA and monitoring history

This deletes PostgreSQL, Qdrant, Redis, and raw document volumes. It preserves
Caddy certificates, Prometheus/Alertmanager volumes, model cache, secrets, and
the bind-mounted encrypted backup/rclone state.

First inspect exact targets:

```bash
docker volume inspect \
  rag-chatbot_postgres_data \
  rag-chatbot_qdrant_data \
  rag-chatbot_redis_data \
  rag-chatbot_document_files
```

Then, only after explicit approval:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml \
  --profile monitoring down
docker volume rm \
  rag-chatbot_postgres_data \
  rag-chatbot_qdrant_data \
  rag-chatbot_redis_data \
  rag-chatbot_document_files
docker compose --env-file deploy/.env -f deploy/compose.yml up -d --build
```

Run `scripts/seed_admin.py` again because the database is empty.

### Full Compose-volume reset

This additionally deletes Caddy's internal CA/config and monitoring history.
Every LAN client must trust the newly generated Caddy root afterward.

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml \
  --profile monitoring --profile operations \
  down --volumes --remove-orphans
docker compose --env-file deploy/.env -f deploy/compose.yml up -d --build
```

`down --volumes` does not delete:

- `deploy/secrets/`;
- `deploy/.env`;
- `model-cache/`;
- bind-mounted `deploy/state/restic` or `deploy/state/rclone`;
- ignored evidence under `artifacts/`.

Keep the encrypted backup repository and OAuth state unless destroying recovery
capability is a separate, explicitly approved action. If they must be retired,
move them to a restricted quarantine path first rather than deleting the only
copy.

## 17. Incident quick reference

| Symptom | First actions |
|---|---|
| Login/account compromise | Disable user, revoke tokens, preserve redacted logs |
| Ingestion stuck | Check job/heartbeat, PostgreSQL/Redis/Qdrant/TEI, restart worker, let reconciliation act |
| Ingestion failed | Inspect bounded status and worker traceback; fix cause; re-upload or retry failed job |
| Readiness 503 | Health remains liveness; inspect dependency gauges/logs internally |
| Qdrant/PostgreSQL mismatch | Stop user traffic, preserve all stores, run `verify-live`, prefer consistent restore |
| Missing raw file | Stop mutations, preserve volumes, restore the matching complete backup set |
| Redis loss | Restart worker/backend and rely on PostgreSQL reconciliation |
| Model OOM | Reduce traffic, inspect GPU, restart pinned model, retain qualified concurrency |
| Backup stale/failed | Inspect user-service journal, verify worker restarted, rerun backup and restore drill |
| Suspected cross-tenant disclosure | Stop Caddy, preserve evidence/backups, run isolation suites before reopening |

The detailed incident procedures are in `docs/incident-runbooks.md`.

## 18. Post-change checklist

After an administrative or deployment change:

1. `docker compose ... config --quiet` passes.
2. Only Caddy publishes application host ports.
3. Required containers are running/healthy.
4. Internal readiness returns 200.
5. HTTPS health works with certificate validation.
6. Database migration head is current.
7. `verify-live` passes when active documents exist.
8. A member can log in and access only their tenant/private sessions.
9. An admin can perform the intended collection/document operation.
10. Ingestion and one scoped chat complete successfully after data/model
    changes.
11. A verified off-machine backup and clean restore drill exist after material
    data/schema changes.
12. Logs and evidence contain no secrets or private content.
