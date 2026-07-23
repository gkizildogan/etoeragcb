# LAN RAG Chatbot

Implementation follows `rag-chatbot-plan.md` phase by phase. P0 qualification
artifacts and immutable pins live in `docs/feasibility.md`,
`docker-images.lock`, and `model-revisions.lock`.

The current deployment scope is LAN-only. P1 provides the FastAPI operational
shell, Streamlit login shell, fully locked private-network Compose stack, and
Caddy internal-CA HTTPS boundary. P2 adds closed administrator-managed accounts,
tenant authorization, rotating refresh tokens, rate limits, and origin checks.
P3 adds private sessions/messages, feedback, collection/document membership,
durable retrieval revisions, and idempotency claim/replay/recovery primitives.
P4 adds staged, versioned ingestion and generation-safe activation. P5–P7 add
bounded planning, tenant-scoped hybrid retrieval, reranking/confidence/context
packing, and SSRF-resistant optional web evidence. P8 adds atomic chat
generation, citation-safe SSE/replay, and tenant/user-scoped signed files. P9
adds the API-only Streamlit client for rotating authentication, private
sessions, safe streaming/citations, feedback, document status, and collection
administration. P10 adds the versioned bilingual golden set, independent
retrieval ablations, reproducible metrics/reporting, feedback export, and the
model-bound calibrated confidence gate. P11 adds bounded cache/backup metrics,
backup-gated data retention, local Prometheus alert rules, pinned
dependency/image audits, resource/load and failure drills, encrypted
Restic+rclone backups, and a clean-volume restore verifier. The local encrypted
restore mechanics and authenticated encrypted Google Drive transfer pass, but
v1 is not declared until a populated document/chat backup passes the strict
off-machine restore drill. Gmail alert delivery and the Python 3.13 runtime
upgrade pass. Public ACME/external isolation and the application host-reboot
drill stay deferred for the selected LAN-only development phase.

Deployment instructions are in `docs/deployment.md`; the current API contract is
summarized by the phase documents through
`docs/p11-operations.md`.

P11 operational checks are available as `deploy/dependency-audit.sh`,
`deploy/security-scan.sh`, `deploy/load-smoke.sh`, and
`deploy/failure-drills.sh`.

Local backend checks:

```bash
cd backend
uv sync --frozen --all-groups
uv run ruff check app tests
uv run mypy app
uv run pytest
```

Streamlit client checks:

```bash
cd streamlit_app
uv sync --frozen --all-groups
uv run ruff check .
uv run pytest
```
