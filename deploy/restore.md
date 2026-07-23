# Restore operator entrypoint

Run from the repository root:

```bash
deploy/restore-drill.sh
```

This downloads the encrypted repository from the authenticated off-machine
remote, restores its newest Restic snapshot into new, isolated PostgreSQL,
Qdrant, and document volumes, verifies the restored state, saves a dated JSON
report under `artifacts/p11/`, and removes the drill project and temporary
repository copy.

The command is strict by default and requires populated account, document,
vector, chat, file, retrieval, and signed-link evidence. For backup-mechanics
development on an empty database only:

```bash
ALLOW_EMPTY_RESTORE=1 deploy/restore-drill.sh
```

That result is explicitly not release eligible. Full preparation, Google Drive
recovery, key handling, invariants, and emergency procedures are documented in
`docs/p11-operations.md` and `docs/incident-runbooks.md`.
