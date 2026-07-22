# P5 planner, metadata resolution, and hybrid retrieval

P5 is an internal retrieval service. It deliberately does not add `/api/chat`;
generation, SSE, citations, and persistence are P8. The P8 handler will call
`app.rag.RetrievalService` with the authenticated tenant and validated explicit
document/collection filters.

## Bounded planning

`VllmPlanner` makes one non-streaming request to the pinned vLLM Chat
Completions endpoint. The request sets `chat_template_kwargs.enable_thinking`
to false, uses deterministic sampling, caps output tokens, and supplies a strict
JSON schema. Pydantic then independently rejects missing, overlong, or unknown
fields and deduplicates bounded hint lists.

Timeouts, HTTP failures, malformed JSON, and schema violations return a bounded
deterministic `knowledge` plan. The fallback preserves at most 1,000 query
characters and extracts at most eight quoted phrases/identifier-like terms. It
never produces metadata scopes. Planner output is only a hint and is never
converted directly into a Qdrant filter.

## Metadata authorization and hint decisions

`MetadataResolver` first loads the tenant's current immutable generation
manifest from PostgreSQL. Explicit document IDs must be in that manifest;
explicit collection IDs must be active in that tenant. Unknown and cross-tenant
IDs return the same `ScopeValidationError`.

Collection membership is resolved from current PostgreSQL rows and compiled to
the manifest's authorized document-version IDs. This makes collection changes
take effect through `retrieval_revision` even if an older Qdrant point carries
its ingestion-time collection payload.

Only metadata inside the explicit authorized scope participates in planner hint
resolution:

- a unique UUID, exact document title/filename, collection name, heading, or
  full section path becomes a hard scope;
- an ambiguous exact match or a close fuzzy match becomes an RRF boost and can
  never widen authorization;
- an unmatched hint has no effect.

Each decision records its type, resolution, matched IDs, and descendant count.
Logs contain only the hint type, decision, counts, and a short SHA-256 digest—not
the raw user hint. A uniquely resolved parent heading expands to descendant
section IDs, but only bounded top chunks and configured local neighbors are
loaded; an entire chapter is never injected.

## Hybrid retrieval and provenance

The query uses the pinned BGE-M3 dense embedding and the same deterministic
lexical sparse representation used during ingestion. Dense and sparse Qdrant
requests run concurrently and receive the identical filter object containing:

- tenant ID;
- active manifest document-version IDs;
- resolved document IDs;
- resolved section IDs when a heading became a hard scope.

The service retains each branch's one-based rank and raw score. It combines
ranks using deterministic reciprocal-rank fusion with `k=60`. Exact-term and
ambiguous/fuzzy metadata matches become additional rank branches rather than
mixing incomparable raw dense and sparse scores.

Qdrant payload is not trusted as the source text. Returned point IDs are
rehydrated from PostgreSQL only when the chunk still matches the same tenant and
active version scope. Candidates include document, section, page/character,
hash, branch-rank, exact-match, hint-match, and neighbor provenance. Initial
results and same-section neighbors are bounded by `RERANK_POOL_N`,
`SECTION_CHUNK_LIMIT`, and `SECTION_NEIGHBOR_RADIUS`. P6 will rerank, remove
near-duplicates, enforce final diversity/token budgets, and apply calibrated
confidence behavior.

## Cache safety

Optional Redis JSON caches fail open. Plan keys bind the tenant, message, and
planner revision. Retrieval keys additionally bind the active generation,
`retrieval_revision`, validated plan, explicit IDs, embedding revision, and
retrieval algorithm signature. Upload activation, tombstoning, and collection
changes therefore select a new key without scanning or deleting old cache keys.

## Verification

The P5 test gate covers:

- valid and malformed vLLM planner output plus deterministic fallback;
- Turkish collection/heading resolution and English semantic evidence;
- exact `ZX-42` ranking and scoped dense+sparse retrieval;
- ambiguous document hints as boosts rather than scopes;
- malicious cross-tenant Qdrant hits rejected during hydration;
- repeated identical text retained on distinct pages;
- bounded heading descendants and neighbor expansion;
- empty/no-answer retrieval;
- cache hits and revision-triggered misses;
- identical Qdrant filters for dense and sparse branches.

The live self-cleaning gate is:

```bash
cd deploy
docker compose run --rm --no-deps backend python -m app.rag.smoke
```

It calls the pinned vLLM planner, BGE-M3 TEI service, and both Qdrant branches,
checks cross-tenant exclusion, and deletes all random smoke points before exit.
