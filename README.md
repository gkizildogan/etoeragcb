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
administration.

Deployment instructions are in `docs/deployment.md`; the current API contract is
summarized by the phase documents through `docs/p9-streamlit.md`.

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
