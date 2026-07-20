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

Deployment instructions are in `docs/deployment.md`; the current API contract is
summarized in `docs/p3-api.md`.

Local backend checks:

```bash
cd backend
uv sync --frozen --all-groups
uv run ruff check app tests
uv run mypy app
uv run pytest
```
