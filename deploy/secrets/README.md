# Deployment secrets

Create these files before the first `docker compose up`. They are ignored by
Git and mounted read-only under `/run/secrets`:

- `postgres_password`: a random PostgreSQL password.
- `database_url`: exactly
  `postgresql+asyncpg://rag:<URL-encoded-password>@postgres:5432/rag`.
- `jwt_secret`: at least 32 random bytes, preferably 64.
- `signing_secret`: a different value of the same strength.
- `backup_encryption_key`: the age or backup-tool key path reserved for P11.

Use mode `0640`, owned by the deployment operator and the group configured as
`SECRETS_GID`. Only that operator/group may contain host accounts. The
non-root backend and PostgreSQL containers receive this GID as a supplemental
read-only group. Store recoverable copies in the deployment secret manager,
and never put secret values in `deploy/.env` or shell history.
