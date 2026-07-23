from __future__ import annotations

from collections.abc import Iterable

from app.evaluation.schemas import (
    CalibratedThresholds,
    CalibrationEvaluation,
    ClassificationMetrics,
    GateObservation,
)


def sweep_thresholds(
    observations: tuple[GateObservation, ...],
    *,
    precision_target: float,
    recall_target: float,
) -> CalibrationEvaluation:
    if not observations or not any(item.expected_answer for item in observations):
        raise ValueError("calibration requires positive and negative observations")
    top_values = _score_candidates(
        item.top_score for item in observations if item.top_score is not None
    )
    margin_values = _score_candidates(
        item.score_margin for item in observations if item.score_margin is not None
    )
    exact_values = _score_candidates(
        item.best_exact_score for item in observations if item.best_exact_score is not None
    )
    evidence_values = tuple(
        range(1, max(1, min(12, max(item.evidence_count for item in observations))) + 1)
    )
    best: (
        tuple[
            tuple[float, ...],
            tuple[float, float, float, int],
            tuple[int, int, int, int, float, float, float],
        ]
        | None
    ) = None
    combinations = 0
    for top_score in top_values:
        for margin in margin_values:
            for exact_score in exact_values:
                for min_evidence in evidence_values:
                    combinations += 1
                    raw_metrics = _raw_classification(
                        observations,
                        top_score=top_score,
                        margin=margin,
                        exact_score=exact_score,
                        min_evidence=min_evidence,
                    )
                    precision, recall, f1 = raw_metrics[4:]
                    targets_met = precision >= precision_target and recall >= recall_target
                    score = (
                        float(targets_met),
                        f1,
                        precision,
                        recall,
                        top_score + margin + exact_score,
                        float(min_evidence),
                    )
                    if best is None or score > best[0]:
                        best = (
                            score,
                            (top_score, margin, exact_score, min_evidence),
                            raw_metrics,
                        )
    if best is None:  # pragma: no cover - candidate grids are always non-empty
        raise RuntimeError("threshold sweep did not evaluate a candidate")
    thresholds = CalibratedThresholds(
        top_score_min=best[1][0],
        score_margin_min=best[1][1],
        exact_score_min=best[1][2],
        min_evidence=best[1][3],
    )
    raw_metrics = best[2]
    metrics = ClassificationMetrics(
        true_positive=raw_metrics[0],
        false_positive=raw_metrics[1],
        true_negative=raw_metrics[2],
        false_negative=raw_metrics[3],
        precision=raw_metrics[4],
        recall=raw_metrics[5],
        f1=raw_metrics[6],
    )
    return CalibrationEvaluation(
        thresholds=thresholds,
        metrics=metrics,
        precision_target=precision_target,
        recall_target=recall_target,
        targets_met=(metrics.precision >= precision_target and metrics.recall >= recall_target),
        combinations_evaluated=combinations,
        observations=observations,
    )


def classification_metrics(
    observations: tuple[GateObservation, ...],
    thresholds: CalibratedThresholds,
) -> ClassificationMetrics:
    true_positive = false_positive = true_negative = false_negative = 0
    for observation in observations:
        predicted = gate_answer(observation, thresholds)
        if predicted and observation.expected_answer:
            true_positive += 1
        elif predicted:
            false_positive += 1
        elif observation.expected_answer:
            false_negative += 1
        else:
            true_negative += 1
    precision = (
        true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    )
    recall = (
        true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    )
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return ClassificationMetrics(
        true_positive=true_positive,
        false_positive=false_positive,
        true_negative=true_negative,
        false_negative=false_negative,
        precision=precision,
        recall=recall,
        f1=f1,
    )


def gate_answer(
    observation: GateObservation,
    thresholds: CalibratedThresholds,
) -> bool:
    if (
        observation.best_exact_score is not None
        and observation.best_exact_score >= thresholds.exact_score_min
    ):
        return True
    return (
        observation.evidence_count >= thresholds.min_evidence
        and observation.top_score is not None
        and observation.top_score >= thresholds.top_score_min
        and observation.score_margin is not None
        and observation.score_margin >= thresholds.score_margin_min
    )


def _raw_classification(
    observations: tuple[GateObservation, ...],
    *,
    top_score: float,
    margin: float,
    exact_score: float,
    min_evidence: int,
) -> tuple[int, int, int, int, float, float, float]:
    true_positive = false_positive = true_negative = false_negative = 0
    for observation in observations:
        exact_pass = (
            observation.best_exact_score is not None and observation.best_exact_score >= exact_score
        )
        threshold_pass = (
            observation.evidence_count >= min_evidence
            and observation.top_score is not None
            and observation.top_score >= top_score
            and observation.score_margin is not None
            and observation.score_margin >= margin
        )
        predicted = exact_pass or threshold_pass
        if predicted and observation.expected_answer:
            true_positive += 1
        elif predicted:
            false_positive += 1
        elif observation.expected_answer:
            false_negative += 1
        else:
            true_negative += 1
    precision = (
        true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    )
    recall = (
        true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    )
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return (
        true_positive,
        false_positive,
        true_negative,
        false_negative,
        precision,
        recall,
        f1,
    )


def _score_candidates(values: Iterable[float]) -> tuple[float, ...]:
    materialized = list(values)
    candidates = {0.0, 1.0}
    for value in materialized:
        rounded = round(value, 6)
        candidates.add(rounded)
        candidates.add(min(1.0, round(rounded + 0.000001, 6)))
    return tuple(sorted(candidates))
