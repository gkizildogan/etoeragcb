# Account and incident runbooks

## Compromised or lost account

1. Disable the account immediately:

   ```bash
   docker compose --env-file deploy/.env -f deploy/compose.yml exec backend \
     python -m app.admin_cli disable-user \
     --actor-email ADMIN_EMAIL --email AFFECTED_EMAIL
   ```

2. The command increments `auth_version` and revokes refresh tokens. Every API
   request rechecks active user state, so existing access tokens stop working.
3. Preserve redacted auth/event logs and the incident time. Never paste
   passwords, bearer tokens, signed URLs, message contents, or secret files
   into a ticket.
4. Investigate tenant membership and relevant access logs.
5. Reset the password while disabled. Re-enable only after the cause is
   resolved and the user confirms account control.
6. For suspected signing/JWT secret exposure, rotate the affected secret in a
   planned outage, recreate backend/worker containers, revoke every refresh
   session, and confirm old tokens/links fail.

Role changes and password resets also revoke sessions. Two administrators
should review superuser promotion and tenant-admin assignment.

## Suspected cross-tenant disclosure

1. Stop user traffic at Caddy if disclosure is ongoing; keep data services
   private.
2. Preserve logs and database backups before remediation.
3. Identify tenant/resource IDs internally without sharing content.
4. Run the exhaustive auth, P3, P8, and P11 isolation tests.
5. Do not re-enable access until the failing authorization path is fixed and
   regression-tested.
6. Notify affected users according to applicable policy/law; this is an owner
   decision, not an automated application action.

## Stuck or interrupted ingestion

1. Inspect document/job state and worker logs. Content and exception detail
   should remain redacted.
2. Check PostgreSQL, Redis, Qdrant, and TEI readiness.
3. Restart the worker. Startup reconciliation safely retries stale
   staged/queued/processing jobs.
4. Confirm the prior generation remains active while repair runs.
5. Run `python -m app.operations.backup verify-live` after recovery.
6. Never manually activate a preparing generation or delete its prior active
   generation.

## Qdrant/PostgreSQL drift

1. Remove the service from user traffic by stopping Caddy or the backend.
2. Run the live consistency command and preserve its JSON.
3. Do not delete “extra” points until active and retained PostgreSQL manifests
   are known.
4. Prefer restoring all stores from one verified backup set. A PostgreSQL-only
   or Qdrant-only rollback can create a new mismatch.
5. Use `deploy/restore-drill.sh` before replacing production volumes.

## Backup stale or failed

1. A failed run must not update `last-success.json`; confirm
   `rag_backup_status=0` or excessive backup age.
2. Read the user-service journal and identify whether dump, snapshot, raw-file
   validation, Restic, OAuth, transfer, or remote check failed.
3. Confirm plaintext staging was removed and the ingestion worker restarted.
4. Resolve the cause, run a manual backup, then run the clean restore drill.
5. Do not bypass backup-gated GC or fabricate a success marker.
6. If the Drive folder was public, make it Restricted, revoke the affected
   OAuth token, rotate the Restic password for future repositories, and assess
   exposure even though repository contents were encrypted.

## Dependency or model outage

1. Readiness should become 503 while health remains a process-liveness signal.
2. Check the specific internal container; no dependency port should be opened
   on the host as a workaround.
3. Restart only the failed pinned service and wait for readiness.
4. For model OOM, preserve GPU/resource evidence, reduce active load, and keep
   generation concurrency at the P0-qualified limit.
5. Run bilingual/citation/planner and retrieval smoke before resuming traffic.

## Restore emergency

1. Preserve the failed host/volumes. Do not initialize a new repository over
   the only backup.
2. Recover the Restic password and rclone OAuth config through the documented
   offline process.
3. Run a clean restore drill first and inspect its report.
4. Restore PostgreSQL, Qdrant, and raw files from the same snapshot.
5. Validate accounts, memberships, active versions, chats, files, vector
   counts, retrieval, and signed links before switching Caddy.
6. Record recovery point/time and all operator actions.
