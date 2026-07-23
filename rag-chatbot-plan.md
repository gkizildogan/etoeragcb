# Public RAG Chatbot — End-to-End Implementation Plan (v4)

**Audience:** a coding agent. Execute P0 through P11 in order. Each phase ends in a mandatory acceptance gate; do not advance while its gate is failing. The system is a small, closed-registration, multi-user public service with one initial shared document tenant, separate accounts, and private chat sessions.

## 0. Non-negotiable rules

1. Do not use orchestration frameworks (LangChain, LlamaIndex, Haystack, Semantic Kernel, DSPy, or equivalents). Implement planning, retrieval, fusion, deduplication, context packing, generation, and citation resolution in Python.
2. Use FastAPI/uvicorn, Streamlit, PostgreSQL 16 with async SQLAlchemy/Alembic, Redis 7 with arq, Qdrant, vLLM, multilingual embedding/reranking, and SearXNG. Python is 3.11+ throughout; the UI is only an API client.
3. Expose only Caddy ports 80 and 443 to the public network. Caddy terminates HTTPS and proxies `/api/*` and signed file routes to FastAPI and all other paths to Streamlit. Backend, worker, PostgreSQL, Redis, Qdrant, vLLM, TEI, and SearXNG have no host-published ports and live on internal Docker networks.
4. Public registration is disabled. An administrator creates accounts. Every database row, Qdrant point, cache key, file operation, and endpoint is tenant-scoped server-side. Users share one initial document tenant but own separate chat sessions.
5. Turkish and English are first-class. Store original extracted/chunk text unchanged for display and generation. Separately derive NFKC and language-aware lexical text (including Turkish `İ/I/ı/i` handling) for hashing, sparse retrieval, and exact matching.
6. Langfuse is not part of v1. Use structured JSON logs, request/job IDs, Prometheus metrics, feedback export, and offline evaluation. Do not log passwords, tokens, raw documents, full prompts, or signed URLs.
7. Use `cyankiwi/Qwen3.5-9B-AWQ-4bit` as the sole generation and planning LLM. Pin its checkpoint revision, the vLLM image digest, all other model revisions, and Python dependencies. P0 feasibility results are authoritative: serving flags and empirical limits may be tuned, but no other LLM checkpoint may be substituted.

## 1. Architecture and network boundary

| Service | Role | Network exposure |
|---|---|---|
| `caddy` | Domain routing, automatic HTTPS, secure headers, request limits | Host ports 80/443 only |
| `streamlit` | Login, chat, documents/collections UI | Internal; reached through Caddy |
| `backend` | FastAPI REST and SSE | Internal; reached through Caddy `/api` |
| `worker` | arq ingestion/reconciliation jobs | Internal |
| `postgres` | Durable application state and index generations | Private data network only |
| `redis` | Queue, rate limits, revocation, disposable caches | Private data network only |
| `qdrant` | Dense/sparse chunk index | Private data network only |
| `vllm` | OpenAI-compatible generation/planning | Private model network only |
| `tei-embed`, `tei-rerank` | Multilingual embedding and reranking | Private model network only |
| `searxng` | Internal metasearch | Private egress network only |

Caddy must set HSTS after HTTPS is verified, CSP appropriate to Streamlit, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, and frame restrictions. The backend accepts forwarded client/proto headers only from Caddy. Firewall/security-group rules permit inbound TCP 80/443 only; administration uses a separate authenticated channel such as SSH/VPN.

### 1.1 Authoritative knowledge pipeline

```text
authenticate + authorize tenant/session + claim idempotency key
load bounded private session history
plan(message, history) -> {
  intent, query, exact_terms[], document_hints[], collection_hints[], heading_hints[]
}
if intent is smalltalk/meta: generate without sources and persist
else:
  resolve hints deterministically against tenant metadata
  start document retrieval and, when requested, web retrieval concurrently
  documents = hybrid dense+sparse retrieval by default
    - hard-scope only unambiguous explicit matches
    - otherwise apply bounded boosts
    - matched headings resolve to section IDs and bounded neighboring chunks
  web = safe search/fetch/extract/chunk, or an empty failed branch
  rerank one combined document+web candidate pool
  enforce duplicate, source, document, and domain diversity
  apply calibrated confidence/no-answer gate and context budget
  if web failed, continue with documents; if all evidence fails, clarify/no-answer
assign immutable S1..Sk to packed sources
generate to an internal buffer while emitting only citation-safe text
validate markers, remove/repair invalid markers before their span can be visible
emit the final authoritative answer and citations, then persist atomically
```

`web_search=true` means documents **plus** web, never web instead of documents. Retrieval work is bounded by configured candidate, fetch, per-section, per-document, per-domain, token, and time limits.

### 1.2 Repository layout

```text
backend/app/
  auth/ tenants/ collections/ documents/ sessions/ chat/
  rag/{planner,resolver,retriever,reranker,gate,dedup,budget,prompts,citations,websearch,pipeline}.py
  ingest/{parsers,sections,chunker,normalization,hashing,embedder,indexer,jobs,reconcile}.py
  core/{db,qdrant,cache,llm,metrics,logging,security,idempotency}.py
  models/ schemas/ main.py config.py workers.py
backend/{alembic,tests,pyproject.toml}
streamlit_app/{app.py,api_client.py,sse.py,state.py,views/}
deploy/{compose.yml,Caddyfile,.env.example,backup.sh,restore.md}
scripts/{seed_admin.py,create_user.py,export_feedback.py,eval/}
```

## 2. Configuration and feasibility constraints

Define and validate at startup at least:

```text
PUBLIC_DOMAIN, ACME_EMAIL, ALLOWED_ORIGINS
DATABASE_URL, REDIS_URL, QDRANT_URL
VLLM_BASE_URL, VLLM_MODEL=cyankiwi/Qwen3.5-9B-AWQ-4bit, VLLM_MODEL_REVISION
MAX_MODEL_LEN, MAX_NEW_TOKENS
EMBED_URL, EMBED_MODEL, EMBED_REVISION, EMBED_DIM
RERANK_URL, RERANK_MODEL, RERANK_REVISION
JWT_SECRET, ACCESS_TOKEN_TTL, REFRESH_TOKEN_TTL, SIGNING_SECRET, SIGNED_URL_TTL
CHUNK_TOKENS, CHUNK_OVERLAP, SECTION_CHUNK_LIMIT, SECTION_NEIGHBOR_RADIUS
RETRIEVE_DENSE_N, RETRIEVE_SPARSE_N, RERANK_POOL_N, RERANK_KEEP
HISTORY_TURNS, HISTORY_TOKEN_BUDGET, CONTEXT_TOKEN_BUDGET
WEB_TOP_RESULTS, WEB_FETCH_TIMEOUT, WEB_MAX_BYTES, WEB_ALLOWED_PORTS
UPLOAD_MAX_MB, ALLOWED_MIME, IDEMPOTENCY_TTL
LOGIN_RATE_LIMITS, CHAT_RATE_LIMITS, CACHE_*_TTL
BACKUP_DESTINATION, BACKUP_ENCRYPTION_KEY_FILE, BACKUP_RETENTION
```

Do not commit provisional gate-score constants. P10 writes calibrated values and golden-set provenance to a versioned evaluation config. Token counting uses the pinned serving tokenizer, not `tiktoken`.

### 2.1 P0 hardware/model qualification

Qualify only `cyankiwi/Qwen3.5-9B-AWQ-4bit` on the actual RTX 3090 (24 GB VRAM) and 64 GB RAM through a pinned, Qwen3.5-compatible vLLM image and a commit-pinned checkpoint revision. Serve it text-only with `--language-model-only` only after verifying that flag against the pinned image. Verify AWQ/compressed-tensors loading and every proposed serving flag against the pinned vLLM and checkpoint revisions. Establish an empirically safe context length, GPU-memory utilization, concurrency, and request limits; do not infer larger defaults from the model's size or assume FP8 KV cache, guided JSON, reasoning parsing, prefix caching, or kernel support without measurement.

Test generation and planning requests with thinking explicitly disabled using the pinned Qwen3.5-supported request configuration; do not use a `/nothink` prompt suffix. Validate planner JSON, bilingual output, citation-marker behavior, and absence of reasoning content. Also test the pinned multilingual embedding model `BAAI/bge-m3` and reranker `BAAI/bge-reranker-v2-m3` on their actual serving path and CPU/RAM budget. Record startup time, VRAM/RAM, max stable context, time to first token, tokens/s, stable concurrency, planner JSON validity, embedding throughput, rerank latency, and Turkish/English smoke quality. Tune serving flags and limits when necessary. If the fixed checkpoint cannot pass, record P0 as blocked and do not select another LLM or proceed.

## 3. Durable data model

Create all schema through Alembic. UUID identifiers are server generated.

- `tenants(id, slug unique, name, created_at)`
- `users(id, email unique, password_hash, is_active, is_superuser, failed_login metadata, created_at, disabled_at)`
- `user_tenants(user_id, tenant_id, role[admin,member], primary key(user_id,tenant_id))`
- `collections(id, tenant_id, name, description, created_by, created_at, updated_at, deleted_at, unique active(tenant_id,name))`
- `documents(id, tenant_id, title, source_filename, mime, active_version_id null, created_by, created_at, deleted_at)`
- `document_collections(document_id, collection_id, tenant_id, primary key(document_id,collection_id))`
- `document_versions(id, document_id, version, file_sha256, storage_key, status[staged,processing,ready,active,failed,superseded], page_count, section_count, chunk_count, index_generation_id, error_code, error_detail, created_at, activated_at, unique(document_id,version))`
- `sections(id, tenant_id, document_id, document_version_id, parent_id null, ordinal, level, heading_original, heading_lexical, page_start, page_end, path_original, path_lexical)`
- `chunks(id, tenant_id, document_id, document_version_id, section_id null, occurrence_index, chunk_index, page_start, page_end, char_start, char_end, content_sha256, lexical_sha256, token_count, text_original, text_lexical, created_at)`
- `index_generations(id bigserial, tenant_id, reason, changed_document_version_id null, status[preparing,active,failed], retrieval_revision, created_at, activated_at)` with exactly one current generation recorded by `tenants.active_index_generation_id`
- `index_generation_documents(generation_id, tenant_id, document_id, document_version_id, primary key(generation_id,document_id))` — the immutable active-version manifest for a corpus snapshot
- `ingestion_jobs(id, tenant_id, document_version_id, arq_job_id, status, attempt, heartbeat_at, error, created_at, updated_at)`
- `chat_sessions(id, tenant_id, user_id, title, created_at, updated_at, deleted_at)`
- `messages(id, tenant_id, session_id, user_id, role, content, meta jsonb, client_request_id null, created_at)`
- `idempotency_requests(tenant_id, user_id, operation, key, request_hash, status, response jsonb, resource_id, expires_at, primary key(...))`
- `refresh_tokens(id, user_id, token_hash, family_id, expires_at, revoked_at, replaced_by)`
- `feedback(id, tenant_id, message_id, user_id, rating, comment, created_at, unique(message_id,user_id))`

Enforce composite tenant-aware foreign keys or equivalent constraints wherever possible. Session queries always include both `user_id` and `tenant_id`; tenant peers cannot read one another's chats.

Chunk IDs must distinguish repeated identical passages: `uuid5(namespace, "{document_version_id}:{page_start}:{section_id-or-none}:{char_start}:{occurrence_index}:{content_sha256}")`. `occurrence_index` is assigned deterministically in document order among equal normalized hashes. IDs are repeatable for retries of the same staged version and cannot collide when identical text occurs on different pages.

Qdrant payload includes `tenant_id`, `created_generation_id`, `document_id`, `document_version_id`, `collection_ids`, `section_id`, `section_path_original`, `section_path_lexical`, page/character spans, occurrence, hashes, `text_original`, and `text_lexical`.

## 4. Indexing and activation semantics

Use one Qdrant collection with named dense and sparse vectors and payload indexes for tenant, generation, document, version, collections, section, pages, hashes, and lexical full text.

Uploads are staged and never change the active version in place:

1. Validate MIME, magic bytes, extension, size, and tenant quota; persist the raw file under a non-public storage key.
2. In one transaction, reserve the idempotency key and create the document/version/job as `staged`. The same key plus identical request returns the original response; reuse with different content returns 409.
3. Parse into original page blocks, infer hierarchical heading/section records, then derive lexical forms separately. Chunk within section/page boundaries and assign occurrence-aware IDs.
4. Index the staged version's points with its `document_version_id`, create a durable `preparing` generation, and construct its manifest by copying the prior generation's active document versions while replacing this document's entry. Validate counts and sample point readability before marking the version `ready`.
5. Atomically activate in PostgreSQL: set the tenant's active generation, the document's active version, and mark the previous version superseded. Only after commit may caches observe the new generation. Retrieval loads that generation's immutable manifest and filters Qdrant by its authorized active `document_version_id` values, so partial/stale versions are invisible while unchanged documents remain searchable without re-embedding.
6. If any step fails, mark the staged version/generation failed and leave the prior active version and generation serving. Garbage-collect inactive points/files only after retention and backup rules permit it.

On worker startup and periodically, reconcile staged/processing jobs with stale heartbeats, PostgreSQL generations, Qdrant points, and arq state. Retry idempotently or fail visibly; finish a proven indexed activation transaction when safe. Redis loss or a reboot must not lose the authoritative generation or make a partial version active.

## 5. Retrieval design

### 5.1 Bounded retrieval planner

One thinking-disabled, schema-constrained call emits:

```json
{
  "intent": "smalltalk|meta|knowledge",
  "query": "standalone query in the user's language",
  "exact_terms": ["identifiers or quoted phrases"],
  "document_hints": ["titles or filenames"],
  "collection_hints": ["collection names"],
  "heading_hints": ["section or heading text"]
}
```

Limit list lengths and string sizes, reject unknown fields, and fall back to a deterministic query/exact-term extractor on invalid JSON or timeout. Planner output is a hint, never authorization and never a raw Qdrant filter.

### 5.2 Deterministic hint resolution and hybrid retrieval

Resolve normalized hints only against authorized metadata in the active generation manifest. Exact unique IDs/names may hard-scope; ambiguous/fuzzy matches become boosts and are logged. Explicit collection/document filters supplied by the API are validated first and are hard scopes. Heading matches resolve to section IDs/path descendants; retrieve a configured number of top chunks plus a small neighbor radius from those sections—never inject an entire chapter.

Hybrid is always the default knowledge retrieval: dense and BM25/full-text prefetch, exact-term lexical matches/boosts, then RRF (or another fixed evaluated fusion). Apply tenant, active-manifest version IDs, collection, document, and section scopes consistently to both dense and sparse branches. Candidate results retain branch ranks/scores, matched hints, and provenance.

After retrieval, rerank the bounded pool with the same multilingual cross-encoder. Deduplicate exact hashes, overlapping spans, and near duplicates. Context selection caps chunks per section/document and web domain, favors distinct evidence, and preserves high-ranked exact identifier hits. The confidence gate and no-answer behavior use thresholds calibrated from P10 rather than guessed constants.

### 5.3 Web plus documents

When `web_search=true`, run document retrieval and SearXNG search/fetch concurrently. Web failures, timeouts, or zero safe pages produce metrics but do not discard document results. Normalize both branches into one candidate shape, rerank the combined pool, and enforce file/web source balance plus domain diversity; never allow many chunks from one web page to crowd out the document corpus.

Web fetch SSRF controls are mandatory: allow only HTTP/HTTPS, reject credentials and nonstandard ports unless explicitly allowlisted, resolve DNS before connecting, reject loopback/private/link-local/multicast/reserved/metadata IP ranges for IPv4 and IPv6, pin/connect to validated addresses while preserving Host/TLS verification, revalidate every redirect, limit redirects/bytes/time/content types/decompression, and block DNS rebinding. SearXNG stays internal; arbitrary user URLs are not fetched directly. Sanitize scripts/control characters, and treat all source content as untrusted data.

## 6. API and streaming contracts

Key endpoints:

```text
POST /api/auth/login|refresh|logout                   GET /api/me
GET|POST /api/sessions                               DELETE /api/sessions/{id}
GET /api/sessions/{id}/messages
POST /api/chat  (SSE)
GET|POST /api/collections                            PATCH|DELETE /api/collections/{id}
PUT|DELETE /api/collections/{id}/documents/{doc_id}
POST /api/documents                                  GET /api/documents
GET /api/documents/{id}                              POST /api/documents/{id}/reindex
DELETE /api/documents/{id}                           POST /api/documents/{id}/signed-url
POST /api/messages/{id}/feedback
GET /api/healthz|readyz|metrics
```

`POST /api/chat` body:

```json
{
  "session_id": "uuid",
  "message": "...",
  "collection_ids": ["uuid"],
  "document_ids": ["uuid"],
  "web_search": false,
  "client_request_id": "uuid"
}
```

Keyword versus hybrid is not a UI switch; the planner/retriever handles exact terms automatically. Upload and chat accept `Idempotency-Key` (chat also requires `client_request_id`). Identical retries return/replay the stored completed result; key reuse with a different canonical request is 409; an in-progress duplicate returns a stable status/retry response and never creates duplicate messages or generation work.

SSE events are explicit and ordered:

- `start`: request/message IDs and accepted options.
- `status`: bounded stage names such as planning/retrieving/reranking/generating; no sensitive internals.
- `delta`: citation-safe answer text only.
- `replace`: authoritative complete answer when buffering was necessary or the streamed representation must be corrected.
- `citations`: validated citation objects keyed by markers.
- `done`: persisted message ID, route, and usage summary.
- `error`: stable error code and retryability; no stack traces.

Invalid citation markers must never remain visible in the final output. Implement a streaming citation sanitizer that holds an incomplete `[`/marker suffix and validates complete `[S<number>]` tokens before emission. Because broader citation repair may change text, the server retains the authoritative buffer, strips invalid markers, emits `replace` before `citations`/`done` if streamed text differs, and persists exactly the final client-visible answer. Clients must implement `replace`, reconnect/idempotent replay, and unknown-event tolerance. Only generation `content`, never reasoning content, enters this path.

File citations use short-lived signed URLs. `POST /documents/{id}/signed-url` verifies tenant, membership, document/version access, and optional page, then returns a Caddy-routed `/api/files/{opaque-token}` URL. The HMAC-signed token binds tenant, user (or deliberate tenant audience), document version/storage key, expiry, and nonce; the backend revalidates it and streams with safe content disposition. Raw storage and direct backend file paths are never public. Expired, modified, cross-tenant, and disabled-user links fail.

## 7. Authentication and public security

- No public `/register`. Provide authenticated superuser administration/CLI for create, disable, role assignment, password reset, and refresh-token revocation.
- Argon2id passwords; short-lived access JWTs; hashed, rotating refresh tokens with reuse detection and family revocation. Account disablement takes effect on every authenticated request and invalidates refresh sessions.
- Mandatory rate limiting for login by normalized account and trusted client IP, with progressive delay/generic responses; also limit refresh, chat, upload, signed-link generation, and concurrent generation. Bound request bodies and queues.
- Enforce exact allowed origins and hostnames. For cookie-less bearer-token clients, still reject unapproved `Origin` on state-changing and SSE requests; if cookies are introduced, add `Secure`, `HttpOnly`, `SameSite` and CSRF tokens. Configure CORS narrowly; never use wildcard with credentials.
- Validate tenant membership server-side for every ID and use non-enumerating 404/403 behavior. Test session ownership separately from document-tenant access.
- Run containers unprivileged with read-only filesystems where possible, dropped capabilities, resource limits, private volumes, dependency/image scanning, secrets through deployment secret files, and egress restricted to the web-fetch/search components.

## 8. UI behavior

Streamlit remains a thin client. It provides login/logout, tenant selection when applicable, private session CRUD, chat streaming, validated citation display, feedback, and a Documents view with upload/version status plus collection creation/rename/delete and document membership/tags. The Chat view exposes only a `Search the web too` checkbox; retrieval mode stays automatic.

The client honors every SSE event, applies `replace`, stores no durable refresh credential in browser-visible state, refreshes once on 401, and clears state on logout/disablement. File citation clicks first request a signed URL, then open it at the cited page when the browser supports page fragments.

## 9. Caching, observability, and backups

The active PostgreSQL generation ID and retrieval revision are part of every retrieval-dependent cache key. Embeddings may be keyed by pinned model revision plus lexical/original text hash; planning by bounded history/message hash; retrieval by tenant/generation/query/scopes; reranking by model revision/query/candidate hashes; answers by the full prompt/model/options. Redis is never the source of truth. Activation invalidates naturally by changing the durable generation. Collection membership changes update affected Qdrant payloads idempotently and create a new manifest/retrieval revision before becoming visible; metadata-only hint changes increment the durable retrieval revision.

Emit structured logs and Prometheus metrics for request/stage latency, queues, ingestion transitions, reconciliation, planner fallback, retrieval branches, gate decisions, web failures, SSRF blocks, source/domain mix, cache hit ratios, citation repairs, auth throttles, and model usage. Store bounded candidate/provenance metadata with assistant messages and export feedback to JSONL. Redact content and secrets by default.

Nightly backups include a transactionally consistent PostgreSQL dump, Qdrant snapshot(s) tied to recorded generation metadata, and raw files/manifests. Encrypt before transfer to authenticated off-machine storage, retain multiple generations, monitor backup age/failure, and document key recovery. A full restore drill on a clean environment must verify accounts, collections, active versions, chats, files, Qdrant generation consistency, retrieval, and signed links. A dump existing is not a passed restore test.

# PHASES

## P0 — Feasibility and pinned baseline

- [ ] Create a minimal Compose GPU/model harness and run the §2.1 qualification for `cyankiwi/Qwen3.5-9B-AWQ-4bit` on the actual host.
- [ ] Pin a Qwen3.5-compatible vLLM image, the exact checkpoint commit in `VLLM_MODEL_REVISION`, verified text-only launch flags, and embedding/reranker revisions and serving images.
- [ ] Verify AWQ/compressed-tensors loading and run explicitly non-thinking Turkish/English generation, guided-plan JSON, citation-marker, embedding, rerank, context-length, reboot, and measured-concurrency smoke tests.
- [ ] Record measurements and chosen limits in `docs/feasibility.md`; set low initial concurrency and safe timeouts.
- **Done when:** the pinned `cyankiwi/Qwen3.5-9B-AWQ-4bit` RTX 3090 configuration starts repeatably after a clean reboot, stays within VRAM/RAM, passes bilingual, citation, planner JSON, and non-thinking interface smoke tests, and handles the configured concurrency without OOM or reasoning leakage. If it cannot pass after serving-limit tuning, P0 is blocked. No replacement LLM or application scaffolding gate may substitute for this test.

## P1 — Scaffolding and public HTTPS boundary

- [ ] Build the repository layout, strict configuration, migrations, CI, health/readiness checks, structured logging, metrics, and pinned Compose stack.
- [ ] Configure Caddy/domain certificates, `/api` and Streamlit routing, secure headers, trusted proxies, internal networks, unprivileged services, volumes, and firewall documentation.
- [ ] Prove only 80/443 are publicly reachable; dependency readiness remains internal/authenticated as appropriate.
- **Done when:** a clean deployment serves the login UI and API through valid HTTPS at the domain, redirecting HTTP, while an external port scan cannot reach application/data/model ports and CI passes.

## P2 — Closed auth, tenancy, and user administration

- [ ] Implement schema, seed/create-admin CLI, admin-managed accounts, login/refresh/logout, disablement, rotation/reuse detection, origin/host checks, and mandatory rate limits.
- [ ] Implement tenant and private-session authorization dependencies plus audit-safe auth logs.
- **Done when:** tests cover throttling, generic login failure, token rotation/reuse, disabled users, disallowed origins, and exhaustive cross-tenant/cross-user resource denial.

## P3 — Sessions, messages, idempotency, and collections

- [ ] Implement private session/message/feedback persistence and cursor pagination.
- [ ] Implement collection CRUD and document membership with tenant-aware constraints.
- [ ] Implement durable idempotency state for upload/chat, request hashing, conflicts, in-progress recovery, and completed replay.
- **Done when:** CRUD and replay tests show no duplicate messages/resources/jobs, collection mutations invalidate retrieval state, and tenant/session ownership is enforced.

## P4 — Staged ingestion, hierarchy, and recovery

- [x] Implement validated upload storage; original/lexical parsing; heading hierarchy; bounded section-aware chunking; occurrence-aware IDs; dense/sparse batches.
- [x] Implement durable generations, staged validation, atomic activation, previous-version retention, deletion/tombstone behavior, and cache revisioning.
- [x] Implement heartbeat reconciliation after worker crash, Redis loss, and reboot; garbage collection must exclude active/retained generations.
- **Done when:** repeated text on different pages has distinct stable IDs; changed uploads activate only after validation; injected failures preserve the previous version; interrupted jobs reconcile after reboot; deleted/old inactive content is not retrieved.

## P5 — Planner, metadata resolution, and hybrid retrieval

- [x] Implement bounded planner plus deterministic fallback, exact/document/collection/heading resolution, and explainable scope-versus-boost decisions.
- [x] Implement dense+sparse hybrid retrieval with identical authorization/generation filters and bounded section-neighbor expansion.
- [x] Add bilingual fixtures for exact identifiers, headings, collections, semantics, repeated page text, scoped hybrid, ambiguous hints, and no-answer queries.
- **Done when:** all retrieval fixtures pass tenant isolation and expected top-k assertions, headings never pull whole chapters, and planner failure still produces safe bounded hybrid retrieval.

## P6 — Combined rerank, confidence, diversity, and budget

- [x] Implement combined candidate reranking, exact/overlap/near-duplicate removal, per-section/document/domain caps, and token budgeting.
- [x] Make the gate consume versioned calibrated configuration and return auditable scores/reasons.
- [x] Property-test budgets and deterministic selection; unit-test source/domain diversity and no-answer outcomes.
- **Done when:** packed context never exceeds limits, relevant exact hits survive diversity, duplicate sources cannot dominate, and the representative golden set supports the configured gate.

## P7 — Safe concurrent web retrieval

- [x] Implement internal SearXNG search, concurrent bounded fetch/extraction, unified candidate records, document fallback, and combined reranking.
- [x] Implement all SSRF/DNS/redirect/content/decompression defenses in §5.3 and egress isolation.
- **Done when:** tests cover document+web merging, multiple-domain diversity, web timeout/failure fallback to documents, malicious redirects, DNS rebinding simulations, IPv4/IPv6 private/metadata targets, oversized/non-HTML responses, and prompt-injection text.

## P8 — Generation, citations, SSE, and signed files

- [x] Implement source IDs/prompts, content-only vLLM streaming, citation-safe buffering/validation, authoritative `replace`, citation objects, persistence, and replay.
- [x] Implement the explicit SSE contract and short-lived tenant/user-scoped signed file route through Caddy.
- **Done when:** fragmented and fabricated markers never remain in the final rendered answer; persisted text equals the client result; reconnect/replay creates no duplicate; signed links open valid citations and reject tampering, expiry, disabled users, and cross-tenant access.

## P9 — Streamlit client

- [x] Implement auth, sessions, SSE/replacement, citations, feedback, errors/empty states, web checkbox, document/version polling, and collection management as pure API calls.
- [x] Add mocked `streamlit.testing.AppTest` coverage and a public-domain manual workflow.
- **Done when:** login → collection creation → upload/activation → scoped question → safe streamed answer → signed citation → feedback works over HTTPS, and web failure visibly falls back without losing document answers.

## P10 — Retrieval evaluation and threshold calibration

- [x] Build a representative bilingual golden set including exact IDs, headings, collections, semantic questions, repeated passages/pages, scoped queries, web+document cases, ambiguous hints, and answerable/unanswerable cases.
- [x] Evaluate sparse-only, dense-only, hybrid, scoped hybrid, and reranked hybrid independently; report recall@k, MRR, nDCG, latency, source diversity, and gate precision/recall.
- [x] Sweep and commit confidence thresholds with dataset/version/model provenance. Export feedback and produce a reproducible Markdown/JSON report; optional LLM judging supplements but does not replace retrieval labels.
- **Done when:** one command reproduces the report, reranked/scoped hybrid meets documented acceptance targets, regressions are enforced in CI where practical, and calibrated thresholds replace all provisional values.

## P11 — Operations, backup, restore, and delivery

- [ ] Add cache metrics, retention/GC jobs, account/runbook procedures, alerts, dependency/image scans, resource/load tests at configured concurrency, and failure playbooks.
- [ ] Automate encrypted off-machine backups and monitoring; execute the full clean-environment restore in §9.
- [ ] Test HTTPS renewal assumptions, host reboot recovery, ingestion interruption, Qdrant/PostgreSQL consistency, cache invalidation, account disablement, cross-tenant access, and signed-link expiry in the release checklist.
- **Done when:** the pinned hardware/model smoke test, public HTTPS/isolation test, retrieval evaluation, and documented full restore drill all pass. Only then is v1 considered delivered.

## 10. Required test matrix (release summary)

| Area | Minimum required evidence |
|---|---|
| Feasibility | Actual RTX 3090 qualification of `cyankiwi/Qwen3.5-9B-AWQ-4bit`: pinned vLLM image/checkpoint revision, AWQ/compressed-tensors load, text-only and non-thinking behavior, context, memory, concurrency, planner JSON, bilingual output, citations, reboot, embedder, and reranker results |
| Retrieval | Sparse/dense/hybrid/scoped/reranked comparisons; IDs, headings, collections, semantics, repeated text, no-answer |
| Web | Concurrent merge, source/domain diversity, document fallback, timeouts and SSRF attempts |
| Ingestion | Idempotent upload, staged failure, atomic activation, previous version, worker interruption and reboot reconciliation |
| API/SSE | Idempotent chat/replay, event ordering, reconnect, fragmented/fabricated citation markers and final replacement |
| Security | Closed registration, throttling, origin/host controls, disablement, tenant/session isolation, signed-link tamper/expiry |
| Persistence | Durable generation/cache invalidation across Redis loss and restart; encrypted off-machine backup and full restore |

## 11. Defaults retained for v1

- One shared initial document tenant; separate administrator-created accounts and private sessions.
- Collections/tags are managed in the Documents UI.
- `web_search=true` requests documents plus web; SearXNG remains internal.
- Hybrid retrieval is automatic; the UI has no keyword-mode selector.
- Langfuse is omitted. vLLM serving flags and empirical resource limits may be tuned through the feasibility/evaluation gates, but `cyankiwi/Qwen3.5-9B-AWQ-4bit` remains the fixed generation and planning checkpoint.
