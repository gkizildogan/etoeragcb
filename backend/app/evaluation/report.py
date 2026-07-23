from __future__ import annotations

import hashlib
from pathlib import Path

import orjson

from app.evaluation.dataset import evaluator_sha256, load_dataset
from app.evaluation.schemas import EvaluationReport
from app.rag.gate import GateConfiguration, load_gate_artifact


class ReportVerificationError(RuntimeError):
    pass


def write_report(
    report: EvaluationReport,
    *,
    json_path: Path,
    markdown_path: Path,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_bytes(
        orjson.dumps(
            report.model_dump(mode="json"),
            option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
        )
        + b"\n"
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")


def write_gate_configuration(
    report: EvaluationReport,
    *,
    path: Path,
) -> None:
    provenance = report.provenance
    thresholds = report.calibration.thresholds
    configuration = GateConfiguration.model_validate(
        {
            "schema_version": 1,
            "artifact_id": "retrieval-gate-v1",
            "calibrated": True,
            "dataset": {
                "name": provenance.dataset_name,
                "version": provenance.dataset_version,
                "sha256": provenance.dataset_sha256,
                "examples": provenance.query_examples,
            },
            "models": {
                "embedding_model": provenance.embedding_model,
                "embedding_revision": provenance.embedding_revision,
                "reranker_model": provenance.reranker_model,
                "reranker_revision": provenance.reranker_revision,
            },
            "thresholds": thresholds.model_dump(mode="json"),
        }
    )
    path.write_bytes(
        orjson.dumps(
            configuration.model_dump(mode="json"),
            option=orjson.OPT_INDENT_2,
        )
        + b"\n"
    )


def load_report(path: Path) -> EvaluationReport:
    try:
        return EvaluationReport.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise ReportVerificationError("cannot load evaluation report") from exc


def verify_report(
    report_path: Path,
    *,
    dataset_root: Path,
    package_root: Path,
    gate_path: Path,
) -> EvaluationReport:
    report = load_report(report_path)
    dataset = load_dataset(dataset_root)
    expected_evaluator_hash = evaluator_sha256(package_root)
    provenance = report.provenance
    failures: list[str] = []
    if provenance.dataset_sha256 != dataset.manifest.dataset_sha256:
        failures.append("dataset hash")
    if provenance.dataset_version != dataset.manifest.version:
        failures.append("dataset version")
    if provenance.evaluator_sha256 != expected_evaluator_hash:
        failures.append("evaluator hash")
    if provenance.query_examples != len(dataset.queries):
        failures.append("query count")
    if provenance.corpus_records != len(dataset.corpus):
        failures.append("corpus count")
    if not report.acceptance.passed or not all(report.acceptance.checks.values()):
        failures.append("acceptance targets")
    if not report.calibration.targets_met:
        failures.append("calibration targets")
    artifact = load_gate_artifact(gate_path).configuration
    if (
        not artifact.calibrated
        or artifact.thresholds is None
        or artifact.thresholds.model_dump() != report.calibration.thresholds.model_dump()
    ):
        failures.append("gate thresholds")
    if (
        artifact.dataset.sha256 != provenance.dataset_sha256
        or artifact.dataset.examples != provenance.query_examples
    ):
        failures.append("gate dataset provenance")
    if artifact.models.model_dump() != {
        "embedding_model": provenance.embedding_model,
        "embedding_revision": provenance.embedding_revision,
        "reranker_model": provenance.reranker_model,
        "reranker_revision": provenance.reranker_revision,
    }:
        failures.append("gate model provenance")
    if failures:
        raise ReportVerificationError(
            "evaluation report verification failed: " + ", ".join(failures)
        )
    return report


def render_markdown(report: EvaluationReport) -> str:
    provenance = report.provenance
    lines = [
        "# P10 retrieval evaluation",
        "",
        f"- Report: `{report.report_id}`",
        f"- Dataset: `{provenance.dataset_name}` `{provenance.dataset_version}`",
        f"- Dataset SHA-256: `{provenance.dataset_sha256}`",
        f"- Evaluator SHA-256: `{provenance.evaluator_sha256}`",
        f"- Corpus/query records: {provenance.corpus_records}/{provenance.query_examples}",
        (f"- Embedding: `{provenance.embedding_model}` `{provenance.embedding_revision}`"),
        (f"- Reranker: `{provenance.reranker_model}` `{provenance.reranker_revision}`"),
        "",
        "## Independent mode results",
        "",
        "| Mode | Recall@5 | Recall@10 | MRR | nDCG@10 | p50 ms | p95 ms | "
        "Sources@10 | Domains@10 | Types@10 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for mode in report.modes:
        metric = mode.metrics
        lines.append(
            f"| {mode.mode} | {metric.recall_at_5:.3f} | "
            f"{metric.recall_at_10:.3f} | {metric.mrr:.3f} | "
            f"{metric.ndcg_at_10:.3f} | {metric.latency_p50_ms:.1f} | "
            f"{metric.latency_p95_ms:.1f} | "
            f"{metric.mean_unique_sources_at_10:.2f} | "
            f"{metric.mean_unique_domains_at_10:.2f} | "
            f"{metric.mean_source_types_at_10:.2f} |"
        )
    calibration = report.calibration
    thresholds = calibration.thresholds
    classification = calibration.metrics
    lines.extend(
        [
            "",
            "## Confidence calibration",
            "",
            f"- Threshold combinations evaluated: {calibration.combinations_evaluated}",
            f"- Top score minimum: `{thresholds.top_score_min:.6f}`",
            f"- Top-two margin minimum: `{thresholds.score_margin_min:.6f}`",
            f"- Exact-term score minimum: `{thresholds.exact_score_min:.6f}`",
            f"- Minimum packed evidence: `{thresholds.min_evidence}`",
            (
                f"- Precision/recall/F1: {classification.precision:.3f} / "
                f"{classification.recall:.3f} / {classification.f1:.3f}"
            ),
            (
                f"- TP/FP/TN/FN: {classification.true_positive}/"
                f"{classification.false_positive}/{classification.true_negative}/"
                f"{classification.false_negative}"
            ),
            "",
            "## Acceptance",
            "",
        ]
    )
    lines.extend(
        f"- [{'x' if passed else ' '}] `{name}`"
        for name, passed in report.acceptance.checks.items()
    )
    lines.extend(
        [
            "",
            (
                "**PASS**"
                if report.acceptance.passed
                else "**FAIL: the calibrated artifact must not be promoted.**"
            ),
            "",
            "The JSON report contains per-query rankings, scores, latency, grouped "
            "language/category metrics, and every gate observation.",
            "",
        ]
    )
    return "\n".join(lines)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
