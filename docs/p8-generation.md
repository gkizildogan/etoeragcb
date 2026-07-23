# P8 generation, citations, SSE, and signed files

P8 turns the P5–P7 retrieval result into one durable chat transaction. The API
streams citation-safe answer text, persists exactly what the client renders, and
stores the completed event transcript for byte-equivalent idempotent replay.

P10 has calibrated the production confidence artifact against the committed
bilingual golden set. Knowledge requests may now enter grounded generation
when packed evidence passes the bound thresholds; empty, weak, ambiguous, and
model-revision-mismatched evidence remains on the persisted `no_answer` route.

## Chat request and transaction

`POST /api/chat` requires an access token, `Idempotency-Key`, and:

```json
{
  "session_id": "uuid",
  "message": "question",
  "collection_ids": ["uuid"],
  "document_ids": ["uuid"],
  "web_search": false,
  "client_request_id": "uuid"
}
```

The server verifies that the session belongs to the authenticated
tenant/user, claims the canonical request, and commits one user message before
starting the stream. The same `client_request_id` cannot create another user
message or be paired with a different request. An active duplicate returns
HTTP 425 with `Retry-After`; conflicting reuse returns HTTP 409.

At completion, the assistant message and completed idempotency transcript are
committed in one database transaction. A completed retry replays the stored
events and sets `X-Idempotent-Replay: true`; it does not run retrieval or
generation again. A cancelled/failed stream marks only an unfinished claim as
failed, allowing bounded recovery without downgrading a transaction that
already committed.

Assistant metadata contains bounded route, usage, gate, source ranks, hashes,
and provenance. It does not store retrieved passage text a second time.

## SSE contract

The response uses `text/event-stream`, disables proxy buffering, and emits:

1. `start` — request, user-message, session, and accepted option identifiers.
2. `status` — only bounded `planning`, `retrieving`, `reranking`, and
   `generating` stage names.
3. `delta` — citation-safe answer content.
4. `replace` — the complete authoritative answer when final repair differs
   from accumulated deltas.
5. `citations` — a map keyed by markers such as `[S1]`.
6. `done` — assistant message ID, route, and token usage.
7. `error` — a stable code and retryability flag, with no exception detail.

Each event also has a monotonic event ID. P9 clients must accumulate `delta`,
replace the accumulated value on `replace`, ignore unknown event types, and
retry with the same idempotency and client request identifiers.

The vLLM client uses `/v1/chat/completions` streaming with
`chat_template_kwargs.enable_thinking=false`, `reasoning_effort=none`,
`include_reasoning=false`, and `stream_options.include_usage=true`. It reads
only `choices[0].delta.content`; reasoning and tool fields have no path into
the answer. This follows vLLM's pinned
[chat-completion protocol](https://docs.vllm.ai/en/v0.25.1/api/vllm/entrypoints/openai/chat_completion/protocol/)
and its documented [OpenAI-compatible server](https://docs.vllm.ai/en/stable/serving/openai_compatible_server/).

Generation concurrency is bounded by `MAX_GENERATION_CONCURRENCY`. History is
limited by both turns and the serving tokenizer. Before generation, the entire
system/history/question/context prompt is rendered and counted through vLLM's
chat-aware `/tokenize` request and must fit
`MAX_MODEL_LEN - MAX_NEW_TOKENS`; oldest history is removed first.

## Citation safety

P6 context source IDs (`S1`, `S2`, and so on) are the only marker allowlist.
The stream sanitizer:

- holds a trailing `[` or incomplete `[S<number>` across model chunks;
- emits a complete marker only when its source ID is allow-listed;
- removes fabricated and noncanonical markers, including `[S0]`;
- keeps an independent authoritative model-content buffer;
- removes trailing incomplete markers and adjacent duplicate citations during
  final repair;
- emits `replace` before `citations` and `done` whenever that final text differs
  from the accumulated deltas.

Grounded output with no valid citation fails closed to `no_answer`. Citation
objects expose document/version/page identifiers for later signed-link
creation, or the validated final URL for web evidence. The persisted assistant
content is exactly the post-`replace` client result.

## Signed document files

An authenticated tenant member requests a link with:

```text
POST /api/documents/{document_id}/signed-url
```

```json
{
  "document_version_id": "uuid",
  "page": 3
}
```

The returned relative URL uses Caddy's `/api/files/{token}` route and appends a
PDF `#page=N` fragment when requested. The short-lived HMAC token binds:

- tenant and user audience;
- document and exact version;
- file hash and an HMAC binding of the current storage key;
- optional validated page;
- expiry and a random nonce.

The storage key itself is not present in the URL payload. The unauthenticated
file-open route revalidates the user is active and still belongs to the bound
tenant, reloads the document/version, compares the storage binding, checks the
page, resolves the path under the configured storage root, and recomputes the
physical file size and SHA-256 before returning an inline safe content
disposition. Invalid, cross-tenant, disabled-user, altered-file, and tampered
links return 404; expired links return 410.

Both public and LAN Caddy configurations route `/api/files/*` explicitly to
the backend. Raw storage directories are never mounted into Caddy or
Streamlit.

## Observability and verification

P8 adds bounded Prometheus series for chat routes, stage latency, citation
repairs, and prompt/completion token usage. Logs include request identifiers
but not messages, retrieved text, answers, tokens, or secrets.

Run the focused and complete gates:

```bash
cd backend
.venv/bin/pytest -q tests/test_p8.py
.venv/bin/pytest -q
.venv/bin/ruff check app tests
.venv/bin/mypy app
```

`tests/test_p8.py` covers fragmented/fabricated markers, authoritative
replacement, content-only model parsing, exact replay and message counts,
in-progress requests, fail-closed generation, valid files, token tampering,
expiry, cross-tenant audience, physical modification, and user disablement.

Validate the deployment boundary:

```bash
cd deploy
docker compose --env-file .env config --quiet
docker compose --env-file .env exec -T caddy \
  caddy validate --config /etc/caddy/Caddyfile
```
