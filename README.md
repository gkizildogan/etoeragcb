# Public RAG Chatbot

Implementation follows `rag-chatbot-plan.md` phase by phase. P0 qualification
artifacts and immutable pins live in `docs/feasibility.md`,
`docker-images.lock`, and `model-revisions.lock`.

P1 provides the FastAPI operational shell, Streamlit login shell, Alembic
baseline, fully locked Python dependencies, private-network Compose stack,
Caddy HTTPS boundary, structured logs, Prometheus metrics, tests, and CI.
Deployment instructions are in `docs/deployment.md`.

Local backend checks:

```bash
cd backend
uv sync --frozen --all-groups
uv run ruff check app tests
uv run mypy app
uv run pytest
```
