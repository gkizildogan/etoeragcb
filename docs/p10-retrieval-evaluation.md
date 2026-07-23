# P10 retrieval evaluation and calibration

P10 replaces the fail-closed provisional confidence artifact with a
model-pinned, dataset-pinned calibration. Human relevance and answerability
labels remain authoritative; no LLM judge was used.

## Golden set

The committed CC0 synthetic set is
`backend/evaluation/golden/v1`. Its manifest binds 33 corpus records and 26
queries with SHA-256:

```text
179418418952d7a1e8067ddec93673d906dacff065d7a608cf49c806a96a2ee9
```

It contains English and Turkish cases for:

- exact identifiers (`ZX-42`, `ORB-771`);
- unique headings and collection/document scopes;
- cross-language semantic paraphrases;
- identical passages on distinct pages;
- two documents with the ambiguous title `Atlas Notes`;
- combined current web and older document evidence;
- answerable questions; and
- unrelated and deceptively related unanswerable questions.

Grades 1–3 are human-authored. The set contains no production documents,
prompts, feedback, credentials, or personal data. It is a diagnostic project
regression set, not a claim of general-domain quality.

## Reproducible command

From the repository root, with the pinned private model services running:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml run \
  --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  --volume "$PWD/backend:/workspace" \
  --workdir /workspace \
  backend python -m app.evaluation.cli run
```

The bind mount is deliberate: production containers have a read-only root
filesystem, while this controlled calibration command must write the report
and gate artifact back to the workspace. It does not create tenants,
documents, Qdrant points, or application messages.

The evaluator uses:

- the production lexical normalization and sparse hashing;
- exact cosine scoring over the small committed corpus with the pinned
  BGE-M3 embeddings;
- production reciprocal-rank fusion, exact-term rank, ambiguous-hint rank,
  and explicit scope filtering;
- the pinned BGE reranker through the production client;
- production deduplication and context caps; and
- the pinned vLLM serving tokenizer for every tentative packed context.

Exact in-memory dense scoring isolates branch/model quality from approximate
index behavior. P5's separate live smoke covers actual Qdrant named-vector
queries and tenant filters.

The five reported modes are independent:

1. sparse only, global authorized corpus;
2. dense only, global authorized corpus;
3. global dense+sparse RRF;
4. scoped dense+sparse RRF; and
5. scoped hybrid followed by cross-encoder reranking.

Each reports Recall@5/10, MRR, nDCG@10, p50/p95 latency, unique source/domain
counts, and source-type diversity. The JSON also contains per-query rankings,
scores, and language/category breakdowns.

## Results

The committed reports are:

- `backend/evaluation/reports/p10-retrieval-v1.json`
- `backend/evaluation/reports/p10-retrieval-v1.md`

| Mode | Recall@5 | MRR | nDCG@10 | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|
| Sparse only | 0.778 | 0.847 | 0.789 | 0.1 | 0.2 |
| Dense only | 1.000 | 1.000 | 1.000 | 73.7 | 100.2 |
| Hybrid | 0.944 | 0.931 | 0.913 | 74.4 | 101.0 |
| Scoped hybrid | 0.972 | 0.931 | 0.927 | 74.0 | 100.7 |
| Reranked hybrid | 1.000 | 1.000 | 0.991 | 3306.2 | 8575.7 |

Reranking latency reflects the pinned CPU cross-encoder and serving-tokenizer
packing, not only vector ranking. P10 exposed that two concurrent reranker
batches exceeded the service limit; the shared production client now runs
batches sequentially and retries a bounded HTTP 429 using `Retry-After`.

The declared ranking gates all pass:

- reranked Recall@5 ≥ 0.80;
- reranked MRR ≥ 0.75;
- reranked nDCG@10 ≥ 0.75; and
- scoped hybrid Recall@5 ≥ 0.80.

## Confidence gate

The exhaustive sweep evaluated 279,936 combinations at every observed decision
boundary. The selected thresholds are:

```json
{
  "top_score_min": 0.955798,
  "score_margin_min": 0.000143,
  "exact_score_min": 1.0,
  "min_evidence": 1
}
```

On packed contexts they produce:

- precision 1.000;
- recall 0.833;
- F1 0.909; and
- TP/FP/TN/FN = 15/0/8/3.

The required precision/recall are 0.95/0.75. Precision is prioritized by the
acceptance constraint because a confident unsupported answer is more damaging
than a conservative no-answer. The three false negatives are retained in the
JSON report for future dataset/model improvement.

When only one packed source exists, the score margin is measured against a
zero baseline. This makes the configured `min_evidence=1` meaningful while
still requiring the singleton to satisfy both the calibrated top-score and
margin thresholds.

The committed gate artifact binds the dataset name/version/hash, 26 examples,
and exact embedding/reranker model revisions. Runtime model mismatch and empty
context remain fail-closed.

## CI regression gate

Run without model services:

```bash
cd backend
python -m app.evaluation.cli verify
```

CI performs the same check. It rejects a changed dataset, evaluator source,
model provenance, gate threshold, failed metric, or failed acceptance target.
This does not rerun GPU/CPU model inference in hosted CI; changing any bound
input requires an intentional live regeneration of the report.

## Feedback export

Export one tenant through the internal CLI wrapper:

```bash
python3 scripts/export_feedback.py \
  --tenant-id <TENANT_UUID> \
  --output artifacts/p10/feedback.jsonl
```

The output is created with mode `0600`. By default it contains rating/comment,
route/gate/web status, citation markers, and SHA-256 hashes of query and answer
text, but not raw query/answer content or a user identifier. Use
`--include-content` only for an explicitly approved private evaluation
workspace. Existing output is not replaced unless `--overwrite` is given.
Feedback is supplemental and never silently changes the human golden labels
or production thresholds.

## Remaining evidence

The P10 calibration permits normal grounded generation. The P9 credentialed
workflow should now be run by an account holder over LAN HTTPS to validate
their uploaded document, citation open, feedback, and forced web-fallback
experience. That user-data workflow is not replaced by the synthetic
evaluation and is not claimed here.
