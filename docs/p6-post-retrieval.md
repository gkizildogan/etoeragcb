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

## Confidence artifact and P10 hand-off

The production artifact is
`backend/app/rag/calibration/retrieval_gate.v1.json`. It intentionally contains:

```json
{
  "schema_version": 1,
  "calibrated": false,
  "dataset": {"name": null, "version": null, "sha256": null, "examples": 0},
  "thresholds": null
}
```

No provisional production score is committed. Until P10 writes thresholds and complete
golden-set provenance, non-empty evidence returns `no_answer` with
`calibration_unavailable`. An empty context returns `no_candidates`. A calibrated artifact
whose embedding or reranker model/revision differs from the running settings returns
`model_revision_mismatch`.

For a calibrated artifact, the gate supports a labeled exact-identifier threshold and the
combination of a minimum top score, minimum top-two margin, and minimum evidence count.
P10 owns the threshold sweep and replaces the artifact values; it does not need to alter
the gate implementation.

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
budget properties, input-order determinism, labeled calibrated gate outcomes, empty
contexts, and the production uncalibrated state.

Run the live model smoke from `deploy/`:

```bash
docker compose --env-file .env -f compose.yml run --rm --no-deps \
  backend python -m app.rag.p6_smoke
```

The smoke calls the actual pinned TEI reranker and vLLM tokenizer, checks a bilingual
ZX-42 query, verifies the context budget, and confirms that the pre-P10 production gate
fails closed.
