# Deployment secrets

Create these files before the first `docker compose up`. They are ignored by
Git and mounted read-only under `/run/secrets`:

- `postgres_password`: a random PostgreSQL password.
- `database_url`: exactly
  `postgresql+asyncpg://rag:<URL-encoded-password>@postgres:5432/rag`.
- `jwt_secret`: at least 32 random bytes, preferably 64.
- `signing_secret`: a different value of the same strength.
- `backup_encryption_key`: a high-entropy Restic repository password. Restic
  encrypts and authenticates repository contents before rclone can transfer
  them.
- `smtp_password`: a dedicated 16-character Google app password for the alert
  sender account, never the account's normal password.

Use mode `0640`, owned by the deployment operator and the group configured as
`SECRETS_GID`. Only that operator/group may contain host accounts. The
non-root backend and PostgreSQL containers receive this GID as a supplemental
read-only group. Store recoverable copies in the deployment secret manager,
and never put secret values in `deploy/.env` or shell history.

The Restic password and authenticated rclone configuration are both required
for disaster recovery. Keep an offline recovery copy of each outside this
computer and outside the Google Drive folder. Losing the Restic password makes
the encrypted backup irrecoverable.
