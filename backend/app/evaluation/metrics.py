from __future__ import annotations

import math
from collections.abc import Callable, Iterable

from app.evaluation.schemas import (
    CorpusRecord,
    GoldenQuery,
    QueryRanking,
    RankingMetrics,
)


def ranking_metrics(
    queries: tuple[GoldenQuery, ...],
    rankings: tuple[QueryRanking, ...],
    corpus: tuple[CorpusRecord, ...],
) -> RankingMetrics:
    ranking_by_query = {item.query_id: item for item in rankings}
    positives = tuple(query for query in queries if query.answerable)
    if not positives:
        raise ValueError("ranking metrics require an answerable query")
    recalls_5: list[float] = []
    recalls_10: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    for query in positives:
        ranked = ranking_by_query[query.id].ranked_ids
        relevant = set(query.relevance)
        recalls_5.append(len(relevant.intersection(ranked[:5])) / len(relevant))
        recalls_10.append(len(relevant.intersection(ranked[:10])) / len(relevant))
        first = next(
            (
                index
                for index, candidate_id in enumerate(ranked, start=1)
                if candidate_id in relevant
            ),
            None,
        )
        reciprocal_ranks.append(1.0 / first if first is not None else 0.0)
        ndcgs.append(_ndcg(query.relevance, ranked, 10))

    record_by_id = {item.id: item for item in corpus}
    unique_sources: list[int] = []
    unique_domains: list[int] = []
    source_types: list[int] = []
    for ranking in rankings:
        top = [record_by_id[item] for item in ranking.ranked_ids[:10]]
        unique_sources.append(len({item.document_id for item in top}))
        unique_domains.append(len({item.domain for item in top if item.domain is not None}))
        source_types.append(len({item.source_type for item in top}))
    latencies = sorted(item.latency_ms for item in rankings)
    return RankingMetrics(
        evaluated_answerable_queries=len(positives),
        recall_at_5=_mean(recalls_5),
        recall_at_10=_mean(recalls_10),
        mrr=_mean(reciprocal_ranks),
        ndcg_at_10=_mean(ndcgs),
        latency_p50_ms=_percentile(latencies, 0.50),
        latency_p95_ms=_percentile(latencies, 0.95),
        mean_unique_sources_at_10=_mean(unique_sources),
        mean_unique_domains_at_10=_mean(unique_domains),
        mean_source_types_at_10=_mean(source_types),
    )


def grouped_metrics(
    queries: tuple[GoldenQuery, ...],
    rankings: tuple[QueryRanking, ...],
    corpus: tuple[CorpusRecord, ...],
    *,
    key: Callable[[GoldenQuery], str],
) -> dict[str, RankingMetrics]:
    result: dict[str, RankingMetrics] = {}
    ranking_by_query = {item.query_id: item for item in rankings}
    values = sorted({key(query) for query in queries if query.answerable})
    for value in values:
        subset = tuple(query for query in queries if query.answerable and key(query) == value)
        subset_rankings = tuple(ranking_by_query[query.id] for query in subset)
        result[value] = ranking_metrics(subset, subset_rankings, corpus)
    return result


def _ndcg(relevance: dict[str, int], ranked: tuple[str, ...], limit: int) -> float:
    observed = [relevance.get(candidate_id, 0) for candidate_id in ranked[:limit]]
    ideal = sorted(relevance.values(), reverse=True)[:limit]
    ideal_score = _dcg(ideal)
    return _dcg(observed) / ideal_score if ideal_score else 0.0


def _dcg(grades: Iterable[int]) -> float:
    return float(
        sum((2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(grades, start=1))
    )


def _mean(values: Iterable[int | float]) -> float:
    materialized = list(values)
    return sum(materialized) / len(materialized) if materialized else 0.0


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    position = max(0, math.ceil(len(values) * fraction) - 1)
    return values[position]
