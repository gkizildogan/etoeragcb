# P10 retrieval evaluation

- Report: `p10-retrieval-evaluation-v1`
- Dataset: `etoeragcb-retrieval-golden` `1.0.0`
- Dataset SHA-256: `179418418952d7a1e8067ddec93673d906dacff065d7a608cf49c806a96a2ee9`
- Evaluator SHA-256: `0279f20d78fc59611a063aa36232dbc03440725a75c294cec960222f2afd0648`
- Corpus/query records: 33/26
- Embedding: `BAAI/bge-m3` `5617a9f61b028005a4858fdac845db406aefb181`
- Reranker: `BAAI/bge-reranker-v2-m3` `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`

## Independent mode results

| Mode | Recall@5 | Recall@10 | MRR | nDCG@10 | p50 ms | p95 ms | Sources@10 | Domains@10 | Types@10 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| sparse_only | 0.778 | 0.778 | 0.847 | 0.789 | 0.1 | 0.2 | 5.58 | 0.38 | 1.12 |
| dense_only | 1.000 | 1.000 | 1.000 | 1.000 | 73.7 | 100.2 | 6.96 | 0.42 | 1.15 |
| hybrid | 0.944 | 0.944 | 0.931 | 0.913 | 74.4 | 101.0 | 7.58 | 0.42 | 1.15 |
| scoped_hybrid | 0.972 | 0.972 | 0.931 | 0.927 | 74.0 | 100.7 | 5.04 | 0.42 | 1.15 |
| reranked_hybrid | 1.000 | 1.000 | 1.000 | 0.991 | 3306.2 | 8575.7 | 4.69 | 0.31 | 1.15 |

## Confidence calibration

- Threshold combinations evaluated: 279936
- Top score minimum: `0.955798`
- Top-two margin minimum: `0.000143`
- Exact-term score minimum: `1.000000`
- Minimum packed evidence: `1`
- Precision/recall/F1: 1.000 / 0.833 / 0.909
- TP/FP/TN/FN: 15/0/8/3

## Acceptance

- [x] `reranked_recall_at_5`
- [x] `reranked_mrr`
- [x] `reranked_ndcg_at_10`
- [x] `scoped_recall_at_5`
- [x] `gate_precision`
- [x] `gate_recall`

**PASS**

The JSON report contains per-query rankings, scores, latency, grouped language/category metrics, and every gate observation.
