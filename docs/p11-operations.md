# P11 operations, backup, and restore

## Current gate status

P11 tooling is implemented, but v1 is **not delivered**. On 2026-07-23:

- Cache hit/miss/store/error counters and bounded latency histograms were
  deployed.
- Daily ephemeral retention and generation-safe file/chunk/Qdrant GC were
  deployed. Payload GC refuses to run without a verified off-machine backup
  newer than 36 hours.
- Restic snapshot `94e226858c4132f79dbed3d883a3f9a68a67b227aae3470ae3c9632a1b03262c`
  passed a repository data check and was restored into clean PostgreSQL,
  Qdrant, and document volumes.
- The restore report is
  `artifacts/p11/restore-20260723T152452Z.json`. It matched the source's two
  users, two memberships, one tenant, and two refresh tokens.
- The source had zero collections, documents, active versions, chunks,
  sessions, or messages. The report is therefore `pass_with_empty_data` and
  `release_gate_eligible=false`; it is proof of the mechanism, not the full §9
  restore gate.
- The owner made the target Google Drive folder Restricted. OAuth authorization
  as the personal Drive owner succeeded, and encrypted Restic snapshot
  `8380ab6f0cf1873603bcfe5f2f6e6186d44326ca8cda61735d82ad504e27adab`
  passed the repository check, authenticated upload, and remote `rclone check`.
- The daily `rag-chatbot-backup.timer` user unit is installed and enabled.
- Both production dependency locks passed `pip-audit` 2.10.1 with no known
  vulnerabilities. Both application images and their development environments
  now use Python 3.13; the live containers report `3.13.14`.
- Grype passes the stricter high-severity gate with zero active critical/high
  findings. One exact CPython 3.13.14 suppression documents an NVD CPE
  false-positive for CVE-2026-15308: CPython's own 3.13.14 security changelog
  records the corresponding `gh-153030` fix in that release.
- The 100-request health/resource smoke passed at concurrency 10 with zero
  failures. The reversible Redis/PostgreSQL/Qdrant/worker/Caddy drill passed;
  its consistency result is non-release-eligible because the corpus is empty.
- Public ACME and an external port scan are deferred for LAN-only operation.
  The application host-reboot drill is deferred by the owner.
- Private Alertmanager delivered the explicit SMTP test to the personal Gmail
  inbox with subject `ETOERAGCB WARNING`; its delivery counter was one with
  zero email failures.

The strict restore verifier will not pass until the source contains at least
one active indexed document, retrievable chunk, active account membership, and
a signed-file target. Complete one normal UI upload/activation and one normal
chat before the release restore drill.

## Cache metrics and retention

`/api/metrics` remains private behind Caddy. Prometheus receives:

- `rag_cache_operations_total{namespace,operation,outcome}`
- `rag_cache_operation_duration_seconds{namespace,operation}`
- `rag_backup_status`
- `rag_backup_last_success_timestamp_seconds`
- `rag_backup_age_seconds`

Cache namespaces are allowlisted (`plan`, `retrieval`, `rerank`, `embedding`,
and `answer`); tenant IDs, queries, keys, and content never become labels.
Redis is still disposable and never authoritative.

At 03:15 daily, the worker:

1. Removes refresh-token and idempotency rows that have been expired/revoked
   beyond `EPHEMERAL_RECORD_RETENTION_DAYS`.
2. Reads `/backup-status/last-success.json`.
3. Skips document/vector GC unless that marker proves encryption, repository
   verification, authenticated off-machine upload, and acceptable age.
4. Protects active and `RETAINED_INDEX_GENERATIONS` manifests.
5. Removes only old failed/superseded chunk payloads, vector points, and raw
   files, then records `garbage_collected_at`.

Default inactive-version retention is 35 days. A local-only backup never
unlocks payload GC.

## Google Drive setup

The configured locator is:

```text
gdrive://folder/160wGX3m1iCI3KK-SL2YHTBBt4TIK-jYX
```

The initial authorization was completed on 2026-07-23:

1. In Google Drive, change the folder's **General access** from
   **Anyone with the link** to **Restricted**.
2. Run `deploy/configure-gdrive-backup.sh`.
3. Create the interactive rclone remote with the exact name `gdrive`, backend
   `drive`, full drive scope, and root folder ID
   `160wGX3m1iCI3KK-SL2YHTBBt4TIK-jYX`.
4. Complete Google OAuth as the personal Drive owner.
5. The script verifies authenticated listing and stores the OAuth config as
   `deploy/state/rclone/rclone.conf` with mode `0600`.

The saved OAuth configuration and folder ID were then used for a real encrypted
backup and byte-level remote repository comparison. Re-run the setup only if
Google access is revoked or the destination changes.

The folder URL itself grants no upload capability. Never place plaintext dump,
snapshot, file archive, application secrets, or the Restic password in Drive.
rclone sees only the already-encrypted Restic repository.

## Backup execution and scheduling

Manual local-only rehearsal:

```bash
BACKUP_SKIP_REMOTE=1 deploy/backup.sh
```

Production off-machine run:

```bash
deploy/backup.sh
```

The script uses an exclusive lock, briefly quiesces the API and ingestion
worker, creates a
transactionally consistent PostgreSQL custom dump, snapshots Qdrant, verifies
raw-file identities, writes the active-generation manifest, and restarts the
API and worker before encryption/transfer. It then:

1. stores the staging set in an encrypted/authenticated Restic repository;
2. checks repository integrity;
3. retains the latest `BACKUP_RETENTION` snapshots;
4. syncs only the encrypted repository through rclone;
5. runs `rclone check`;
6. atomically writes `last-success.json`.

Any error exits nonzero, removes plaintext staging, and does not update the
success marker. A SIGINT/SIGTERM trap also restarts the worker.

Install the user timer from `deploy/systemd/`:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/rag-chatbot-backup.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now rag-chatbot-backup.timer
systemctl --user list-timers rag-chatbot-backup.timer
```

The checked-in unit assumes the repository is at
`%h/projects/etoeragcb`; edit both paths if it moves. Enable user lingering if
nightly jobs must run while logged out. Inspect failures with:

```bash
journalctl --user -u rag-chatbot-backup.service
```

## Key recovery

Recovery requires all of:

- `deploy/secrets/backup_encryption_key` or its offline secret-manager copy;
- `deploy/state/rclone/rclone.conf` or a fresh OAuth authorization to the same
  restricted folder;
- PostgreSQL/signing secrets from the deployment secret manager;
- the pinned Compose files/images or compatible recovery tooling.

Test the offline copies periodically. Do not keep the only Restic password on
the backed-up host or in the encrypted repository it unlocks. If the password
is lost, the repository cannot be decrypted. If OAuth is revoked, authorize a
new rclone config; this does not change Restic encryption.

## Clean restore drill

After a successful off-machine backup:

```bash
deploy/restore-drill.sh
```

The strict script downloads the encrypted repository from the authenticated
off-machine remote into a fresh temporary directory, performs a full Restic
data read, restores the newest snapshot, and creates a uniquely named Compose
project with empty PostgreSQL, Qdrant, and document volumes. It restores all
three stores, then verifies:

- table counts including accounts, collections, chats, and feedback;
- each tenant's active generation and manifest;
- every active raw file's size and SHA-256;
- Qdrant counts for every active tenant/version;
- at least one directly retrievable active chunk;
- signed-token binding and expiry rejection.

It writes a dated report under `artifacts/p11/`, records `backup_source` in the
report, and removes only the isolated drill containers/volumes and temporary
remote copy. `ALLOW_EMPTY_RESTORE=1` exists solely to validate mechanics from
the local repository on an empty development database and can never satisfy
the release gate. Set `RESTORE_FROM_REMOTE=1` explicitly to exercise remote
download in an empty-data rehearsal.

## Monitoring and alerts

Alertmanager sends warnings directly through Gmail SMTP; no webhook is involved.
It uses the personal Gmail account as both sender and recipient. The
administrator application account `admin@example.com` is unrelated to SMTP.

First enable 2-Step Verification on the personal Google account and create a
dedicated 16-character Google app password. Store it through the hidden prompt;
never paste it into chat or commit it:

```bash
ALERT_EMAIL=goksu.kizildogan@gmail.com deploy/configure-alert-email.sh
```

The script creates ignored runtime files with restricted permissions:
`deploy/state/alertmanager/alertmanager.yml` and
`deploy/secrets/smtp_password`. Start the private monitoring profile:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml \
  --profile monitoring up -d alertmanager prometheus
```

Prometheus and Alertmanager have no published host ports. Alertmanager alone has
outbound SMTP access; it has no database, JWT, signing, backup, or application
credentials. The fixed subject is `ETOERAGCB WARNING`, and the body contains the
alert status, severity, summary/error, details, and timestamps.

After both containers are healthy, send one explicit test:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml \
  --profile monitoring exec -T alertmanager \
  amtool --alertmanager.url=http://127.0.0.1:9093 alert add \
  alertname=EtoeragcbSmtpTest severity=warning \
  --annotation=summary='SMTP delivery test' \
  --annotation=description='Manual P11 test; no application failure occurred.'
```

Delivery begins after the configured 30-second group wait. The checked rules
alert on dependency failure, missing/stale backups, sustained 5xx rate, cache
errors, and sustained auth throttling. Validate rules and the example receiver
configuration with the pinned `promtool` and `amtool` images in CI.

The explicit delivery test passed on 2026-07-23. The recipient confirmed the
message arrived, and Alertmanager reported one email notification with zero
email failures.

## Security and load checks

CI audits both hashed Python lock files with the SHA-pinned official
`pip-audit` action. It builds and scans both application images with
digest-pinned Grype, failing on active high or critical vulnerabilities.

Local application-image scan:

```bash
deploy/security-scan.sh
```

Set `SCAN_ALL_LOCAL_IMAGES=1` for the full local image inventory. JSON evidence
is written under `artifacts/p11/security/`.

Local production-lock audit:

```bash
deploy/dependency-audit.sh
```

This uses pinned `pip-audit` 2.10.1 with strict hash verification and writes
machine-readable evidence beside the image-scan reports.

On 2026-07-23 both application images were rebuilt from the immutable
`python:3.13.14-slim-trixie` base. The production and development dependency
locks were regenerated for `==3.13.*`; static checks, strict typing, tests, and
the committed P10 calibration verifier passed. Grype reports zero active
critical/high findings under the high-severity failure gate.

`deploy/security/grype.yaml` contains one exact package/version suppression for
CVE-2026-15308. The match is NVD CPE-based and claims the fix starts at 3.15,
but the official CPython 3.13.14 changelog records the same `gh-153030`
denial-of-service fix. Keep the suppression scoped to binary package `python`
version `3.13.14`, and remove it when Grype's advisory data is corrected.

LAN health/resource load:

```bash
deploy/load-smoke.sh health
```

Authenticated configured-concurrency generation smoke:

```bash
LOAD_TEST_EMAIL=member@example.com deploy/load-smoke.sh chat
```

The password is prompted and never accepted on the command line. Chat load is
capped at five requests and concurrency two; the deployed default remains one.

The post-upgrade health run is
`artifacts/p11/load-20260723T163811Z/`: 100/100 HTTP 200 responses at
concurrency 10 with zero failures.

## Failure drills

`deploy/failure-drills.sh` performs reversible service-level drills:

- Redis flush and backend/worker restart;
- PostgreSQL and Qdrant outage detection through readiness;
- dependency recovery;
- ingestion worker SIGKILL and startup reconciliation;
- Caddy restart and CA-verified HTTPS;
- final PostgreSQL/Qdrant/file consistency.

On an empty development database only, set `ALLOW_EMPTY_LIVE=1`; that evidence
is not release eligible. The script deliberately does not reboot the host.

For the selected LAN development phase, manually stop and start the Compose
stack without deleting its volumes:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml \
  --profile monitoring stop
docker compose --env-file deploy/.env -f deploy/compose.yml \
  --profile monitoring up -d
docker compose --env-file deploy/.env -f deploy/compose.yml \
  --profile monitoring ps
```

Do not stop the stack while ingestion or backup is active. This manual
container lifecycle is sufficient for current application testing; it does not
replace the deferred host-reboot recovery gate.

For the actual reboot gate:

1. Ensure no backup or ingestion is running.
2. Save `docker compose ps`, `nvidia-smi`, HTTPS health, and live-consistency
   evidence.
3. Reboot during an approved window.
4. Confirm Docker auto-start, every required container, P0 model smoke,
   readiness, retrieval, and HTTPS.
5. Save the same post-reboot evidence and link it from the release checklist.

Do not automate `sudo reboot` from this repository.
