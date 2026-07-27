from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.evaluation.history import (
    CandidateLabel,
    HistoryEvaluationError,
    QueryLabel,
    read_jsonl,
)

PRECISION_LIMITS = (1, 3, 5, 10)


def load_and_validate_labels(
    run_dir: Path,
    examples: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]],
) -> tuple[dict[str, QueryLabel], dict[tuple[str, str], CandidateLabel]]:
    expected_examples = {_required_string(item, "example_id") for item in examples}
    expected_candidates = {
        (_required_string(item, "example_id"), _required_string(item, "candidate_key"))
        for item in candidates
    }
    query_labels: dict[str, QueryLabel] = {}
    candidate_labels: dict[tuple[str, str], CandidateLabel] = {}
    for line_number, record in enumerate(read_jsonl(run_dir / "labels.jsonl"), start=1):
        try:
            kind = record.get("kind")
            if kind == "query":
                query_label = QueryLabel.model_validate(record)
                query_key = str(query_label.example_id)
                if query_key not in expected_examples:
                    raise HistoryEvaluationError(f"unknown query label {query_key}")
                if query_key in query_labels:
                    raise HistoryEvaluationError(f"duplicate query label {query_key}")
                query_labels[query_key] = query_label
            elif kind == "candidate":
                candidate_label = CandidateLabel.model_validate(record)
                candidate_key = (
                    str(candidate_label.example_id),
                    candidate_label.candidate_key,
                )
                if candidate_key not in expected_candidates:
                    raise HistoryEvaluationError(
                        f"unknown candidate label {candidate_key[0]} / {candidate_key[1]}"
                    )
                if candidate_key in candidate_labels:
                    raise HistoryEvaluationError(
                        f"duplicate candidate label {candidate_key[0]} / {candidate_key[1]}"
                    )
                candidate_labels[candidate_key] = candidate_label
            else:
                raise HistoryEvaluationError(f"unknown label kind at line {line_number}")
        except ValidationError as exc:
            raise HistoryEvaluationError(f"invalid label at line {line_number}: {exc}") from exc
    missing_queries = expected_examples - query_labels.keys()
    missing_candidates = expected_candidates - candidate_labels.keys()
    if missing_queries:
        raise HistoryEvaluationError(f"missing query labels: {', '.join(sorted(missing_queries))}")
    if missing_candidates:
        first = sorted(missing_candidates)[0]
        raise HistoryEvaluationError(f"missing candidate labels: {first[0]} / {first[1]}")
    return query_labels, candidate_labels


def score_history_records(
    examples: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]],
    query_labels: dict[str, QueryLabel],
    candidate_labels: dict[tuple[str, str], CandidateLabel],
) -> dict[str, Any]:
    candidates_by_example: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in candidates:
        candidates_by_example[_required_string(item, "example_id")].append(item)
    per_query = [
        _score_query(
            example,
            candidates_by_example[_required_string(example, "example_id")],
            query_labels[_required_string(example, "example_id")],
            candidate_labels,
        )
        for example in examples
    ]
    answerability = _answerability_metrics(per_query)
    overall = _aggregate_ranking(per_query)
    reranker = _aggregate_reranker(per_query)
    return {
        "schema_version": 1,
        "diagnostic_only": True,
        "judgment": {
            "label_source": "human",
            "relevance_scale": {
                "0": "unrelated",
                "1": "partially useful context",
                "2": "substantially but incompletely supports the answer",
                "3": "directly and completely supports it",
            },
        },
        "examples": len(per_query),
        "ranking": overall,
        "answerability_gate": answerability,
        "reranker_comparison": reranker,
        "breakdowns": _breakdowns(per_query),
        "recorded_view": _recorded_view(per_query),
        "per_query": per_query,
    }


def _score_query(
    example: dict[str, Any],
    candidates: Sequence[dict[str, Any]],
    query_label: QueryLabel,
    labels: dict[tuple[str, str], CandidateLabel],
) -> dict[str, Any]:
    example_id = _required_string(example, "example_id")
    grades = {
        _required_string(item, "candidate_key"): labels[
            (example_id, _required_string(item, "candidate_key"))
        ].relevance
        for item in candidates
    }
    pre = _ordered_keys(candidates, "current_retrieval_rank")
    post = _ordered_keys(candidates, "current_rerank_rank")
    recorded = _ordered_keys(
        [item for item in candidates if item.get("recorded_packed") is True],
        "recorded_rank",
    )
    recorded_pre = _ordered_keys(candidates, "recorded_rank")
    recorded_post = _ordered_keys(candidates, "recorded_rerank_rank")
    pre_metrics = _ranking_for_query(pre, grades, query_label.answerable)
    post_metrics = _ranking_for_query(post, grades, query_label.answerable)
    relevant = {key for key, grade in grades.items() if grade > 0}
    lifts: list[int] = []
    harmful: list[dict[str, Any]] = []
    for key in sorted(relevant):
        pre_rank = _rank(pre, key)
        post_rank = _rank(post, key)
        if pre_rank is not None and post_rank is not None:
            lifts.append(pre_rank - post_rank)
            if post_rank > pre_rank:
                harmful.append(
                    {
                        "candidate_key": key,
                        "pre_rank": pre_rank,
                        "post_rank": post_rank,
                        "demotion": post_rank - pre_rank,
                    }
                )
    current_gate_route = example.get("current_gate_route")
    predicted_answerable = current_gate_route == "answer"
    ndcg_delta = post_metrics["ndcg_at_10"] - pre_metrics["ndcg_at_10"]
    outcome = "win" if ndcg_delta > 1e-12 else ("loss" if ndcg_delta < -1e-12 else "tie")
    return {
        "example_id": example_id,
        "answerable": query_label.answerable,
        "language": query_label.language,
        "role_group": example.get("role_group", "unknown"),
        "scope_type": example.get("scope_type", "unscoped"),
        "feedback": _feedback_group(example.get("feedback_rating")),
        "corpus_comparability": example.get("corpus_comparability", "unknown"),
        "gate_predicted_answerable": predicted_answerable,
        "pre_rerank": pre_metrics,
        "post_rerank": post_metrics,
        "recorded_packed": _ranking_for_query(recorded, grades, query_label.answerable),
        "recorded_evidence_available": bool(recorded),
        "recorded_pool_complete": bool(example.get("recorded_pool_complete")),
        "recorded_pre_rerank": _ranking_for_query(recorded_pre, grades, query_label.answerable),
        "recorded_post_rerank": _ranking_for_query(recorded_post, grades, query_label.answerable),
        "reranker_outcome": outcome,
        "mean_relevant_rank_lift": _mean(lifts),
        "harmful_demotions": harmful,
    }


def _ranking_for_query(
    ranking: Sequence[str], grades: dict[str, int], answerable: bool
) -> dict[str, float]:
    relevant = {key for key, grade in grades.items() if grade > 0}
    precision = {
        f"precision_at_{limit}": (sum(grades.get(key, 0) > 0 for key in ranking[:limit]) / limit)
        for limit in PRECISION_LIMITS
    }
    first = next(
        (index for index, key in enumerate(ranking, start=1) if grades.get(key, 0) > 0),
        None,
    )
    return {
        **precision,
        "judged_pool_recall_at_5": (
            len(relevant.intersection(ranking[:5])) / len(relevant) if relevant else 0.0
        ),
        "judged_pool_recall_at_10": (
            len(relevant.intersection(ranking[:10])) / len(relevant) if relevant else 0.0
        ),
        "mrr": (1.0 / first if first is not None else 0.0),
        "ndcg_at_10": _ndcg(grades, ranking, 10),
        "included_in_answerable_aggregate": float(answerable),
    }


def _aggregate_ranking(per_query: Sequence[dict[str, Any]]) -> dict[str, Any]:
    positives = [item for item in per_query if item["answerable"]]
    return {
        "evaluated_answerable_queries": len(positives),
        "pre_rerank": _mean_metric_maps(item["pre_rerank"] for item in positives),
        "post_rerank": _mean_metric_maps(item["post_rerank"] for item in positives),
    }


def _aggregate_reranker(per_query: Sequence[dict[str, Any]]) -> dict[str, Any]:
    positives = [item for item in per_query if item["answerable"]]
    pre = _mean_metric_maps(item["pre_rerank"] for item in positives)
    post = _mean_metric_maps(item["post_rerank"] for item in positives)
    delta_keys = (
        "precision_at_1",
        "precision_at_3",
        "precision_at_5",
        "precision_at_10",
        "mrr",
        "ndcg_at_10",
    )
    return {
        "deltas": {key: post.get(key, 0.0) - pre.get(key, 0.0) for key in delta_keys},
        "mean_relevant_rank_lift": _mean(item["mean_relevant_rank_lift"] for item in positives),
        "harmful_demotions": sum(len(item["harmful_demotions"]) for item in positives),
        "win_tie_loss": {
            outcome: sum(item["reranker_outcome"] == outcome for item in positives)
            for outcome in ("win", "tie", "loss")
        },
    }


def _answerability_metrics(per_query: Sequence[dict[str, Any]]) -> dict[str, Any]:
    true_positive = [
        item for item in per_query if item["answerable"] and item["gate_predicted_answerable"]
    ]
    false_positive = [
        item for item in per_query if not item["answerable"] and item["gate_predicted_answerable"]
    ]
    false_negative = [
        item for item in per_query if item["answerable"] and not item["gate_predicted_answerable"]
    ]
    true_negative = [
        item
        for item in per_query
        if not item["answerable"] and not item["gate_predicted_answerable"]
    ]
    tp, fp, fn = len(true_positive), len(false_positive), len(false_negative)
    return {
        "true_positive": tp,
        "false_positive": fp,
        "true_negative": len(true_negative),
        "false_negative": fn,
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "false_positive_examples": [item["example_id"] for item in false_positive],
        "false_negative_examples": [item["example_id"] for item in false_negative],
    }


def _breakdowns(per_query: Sequence[dict[str, Any]]) -> dict[str, Any]:
    keys = {
        "role_group": lambda item: str(item["role_group"]),
        "language": lambda item: str(item["language"]),
        "scope_type": lambda item: str(item["scope_type"]),
        "feedback": lambda item: str(item["feedback"]),
        "corpus_comparability": lambda item: (
            "unchanged" if item["corpus_comparability"] == "unchanged" else "changed_or_unknown"
        ),
    }
    return {name: _grouped(per_query, key) for name, key in keys.items()}


def _grouped(
    records: Sequence[dict[str, Any]],
    key: Callable[[dict[str, Any]], str],
) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[key(record)].append(record)
    return {
        name: {
            "examples": len(items),
            "ranking": _aggregate_ranking(items),
            "answerability_gate": _answerability_metrics(items),
            "reranker_comparison": _aggregate_reranker(items),
        }
        for name, items in sorted(groups.items())
    }


def _recorded_view(per_query: Sequence[dict[str, Any]]) -> dict[str, Any]:
    packed_supported = [
        item for item in per_query if item["answerable"] and item["recorded_evidence_available"]
    ]
    complete = [item for item in per_query if item["answerable"] and item["recorded_pool_complete"]]
    legacy = [item for item in packed_supported if not item["recorded_pool_complete"]]
    return {
        "legacy_scope": "stored_packed_sources_only",
        "warning": "Legacy packed sources are not a reranker evaluation.",
        "legacy_evaluated_answerable_queries": len(legacy),
        "packed_source_metrics": _mean_metric_maps(
            item["recorded_packed"] for item in packed_supported
        ),
        "complete_pre_rerank_pool_available": bool(complete),
        "complete_pool_evaluated_answerable_queries": len(complete),
        "complete_pool_pre_rerank": _mean_metric_maps(
            item["recorded_pre_rerank"] for item in complete
        ),
        "complete_pool_post_rerank": _mean_metric_maps(
            item["recorded_post_rerank"] for item in complete
        ),
    }


def _ordered_keys(candidates: Sequence[dict[str, Any]], field: str) -> list[str]:
    ranked = [
        item
        for item in candidates
        if isinstance(item.get(field), int) and not isinstance(item.get(field), bool)
    ]
    ranked.sort(key=lambda item: (int(item[field]), _required_string(item, "candidate_key")))
    return [_required_string(item, "candidate_key") for item in ranked]


def _mean_metric_maps(items: Iterable[dict[str, float]]) -> dict[str, float]:
    materialized = list(items)
    if not materialized:
        return {}
    keys = set.intersection(*(set(item) for item in materialized))
    return {
        key: _mean(item[key] for item in materialized)
        for key in sorted(keys)
        if key != "included_in_answerable_aggregate"
    }


def _ndcg(grades: dict[str, int], ranking: Sequence[str], limit: int) -> float:
    observed = [grades.get(key, 0) for key in ranking[:limit]]
    ideal = sorted(grades.values(), reverse=True)[:limit]
    denominator = _dcg(ideal)
    return _dcg(observed) / denominator if denominator else 0.0


def _dcg(grades: Iterable[int]) -> float:
    return float(
        sum((2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(grades, start=1))
    )


def _rank(ranking: Sequence[str], key: str) -> int | None:
    try:
        return ranking.index(key) + 1
    except ValueError:
        return None


def _feedback_group(value: object) -> str:
    if value == 1:
        return "positive"
    if value == -1:
        return "negative"
    return "none"


def _required_string(record: dict[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise HistoryEvaluationError(f"{field} must be a non-empty string")
    return value


def _mean(values: Iterable[int | float]) -> float:
    materialized = list(values)
    return sum(materialized) / len(materialized) if materialized else 0.0
