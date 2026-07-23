# P6 post-retrieval pipeline

P6 turns the bounded P5 retrieval pool into source-neutral, reranked, deduplicated,
diverse, token-safe evidence. It also produces a fail-closed confidence decision with
enough provenance for P10 to replace the uncalibrated production artifact without a code
change.

## Processing order

1. Document chunks are converted to `EvidenceCandidate`; P7 web pages can use the same
   shape with `source_type=web`, a canonical URL, and a normalized domain.
2. `TeiReranker` sorts by the original retrieval rank, bounds the pool, calls TEI `/rerank`
   in batches of 32, validates one normalized score per input, and sorts deterministically.
   Cache keys contain the query, reranker revision, candidate IDs, and content hashes.
3. `deduplicate` collapses exact content hashes, exact lexical hashes, heavily overlapping
   spans, and high-Jaccard lexical shingles. Each removal records the survivor, reason, and
   similarity. A near/overlap removal cannot discard a candidate that adds a distinct exact
   identifier; exact-hash removals merge the identifier provenance into the survivor.
4. `ContextPacker` considers the best representative for each exact identifier before the
   ordinary rerank order. Accepted sources must satisfy the per-section, per-source,
   per-domain, and total-candidate caps.
5. Every tentative complete context is counted through the pinned generation server's
   `/tokenize` endpoint. A candidate is accepted only when the returned count is within
   `CONTEXT_TOKEN_BUDGET`; the packer never estimates with a different tokenizer.
6. `ConfidenceGate` evaluates only evidence that actually reached the context. Its output
   includes the route, all applicable reasons, observed top/second/margin/exact scores,
   evidence count, artifact hash, and calibration dataset provenance.

The TEI request follows the official [rerank endpoint contract](https://huggingface.co/docs/text-embeddings-inference/en/quick_tour), and context accounting follows vLLM's official [tokenization API](https://docs.vllm.ai/en/latest/api/vllm/entrypoints/serve/tokenize/).

## Limits

The deployment adds these explicit settings:

- `DOCUMENT_CHUNK_LIMIT=6`: maximum packed chunks from one document or canonical source.
- `DOMAIN_CHUNK_LIMIT=2`: maximum packed web chunks from one domain.
- `RETRIEVAL_GATE_CONFIG=/app/app/rag/calibration/retrieval_gate.v1.json`: versioned gate
  artifact loaded by the application.

Existing `SECTION_CHUNK_LIMIT`, `RERANK_POOL_N`, `RERANK_KEEP`,
`CONTEXT_TOKEN_BUDGET`, and `CACHE_RERANK_TTL` remain authoritative. Configuration rejects
a section limit larger than the document/source limit.

## Confidence artifact and P10 calibration

The production artifact is
`backend/app/rag/calibration/retrieval_gate.v1.json`. P6 initially committed a
fail-closed placeholder. P10 has now replaced it with:

```json
{
  "schema_version": 1,
  "calibrated": true,
  "dataset": {
    "name": "etoeragcb-retrieval-golden",
    "version": "1.0.0",
    "examples": 26
  },
  "thresholds": {
    "top_score_min": 0.955798,
    "score_margin_min": 0.000143,
    "exact_score_min": 1.0,
    "min_evidence": 1
  }
}
```

The artifact contains the complete dataset SHA-256 and exact embedding/reranker
revisions. An empty context returns `no_candidates`; a model mismatch returns
`model_revision_mismatch`. Both remain fail-closed.

The gate supports a labeled exact-identifier threshold and the combination of
a minimum top score, minimum top-two margin, and minimum evidence count. For a
singleton context, its margin is measured against zero so `min_evidence=1` is
usable. See `docs/p10-retrieval-evaluation.md` for the sweep and evidence.

## Verification

Run the static and complete unit gates:

```bash
cd backend
UV_CACHE_DIR=/tmp/etoeragcb-uv-cache uv run ruff check app tests
UV_CACHE_DIR=/tmp/etoeragcb-uv-cache uv run mypy app
UV_CACHE_DIR=/tmp/etoeragcb-uv-cache uv run pytest -q
```

`tests/test_p6.py` covers TEI batching and cache behavior, serving-tokenizer requests,
every duplicate class, exact-hit preservation, section/source/domain caps, generated
budget properties, input-order determinism, labeled calibrated gate outcomes,
empty contexts, and committed P10 provenance.

Run the live model smoke from `deploy/`:

```bash
docker compose --env-file .env -f compose.yml run --rm --no-deps \
  backend python -m app.rag.p6_smoke
```

The smoke calls the actual pinned TEI reranker and vLLM tokenizer, checks a
bilingual ZX-42 query, verifies the context budget, and confirms P10 dataset
provenance is active.
