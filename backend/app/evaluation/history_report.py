from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson

from app.evaluation.history import (
    HistoryEvaluationError,
    assert_private_run,
    now_rfc3339,
    read_jsonl,
    run_file_hashes,
    sha256_file,
    write_private_bytes,
    write_private_json,
)
from app.evaluation.history_scoring import load_and_validate_labels, score_history_records


def score_history_run(run_dir: Path) -> dict[str, Any]:
    assert_private_run(run_dir)
    manifest_path = run_dir / "manifest.json"
    try:
        manifest = orjson.loads(manifest_path.read_bytes())
    except (OSError, orjson.JSONDecodeError) as exc:
        raise HistoryEvaluationError("manifest.json is missing or invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise HistoryEvaluationError("unsupported historical run manifest")
    _verify_immutable_files(run_dir, manifest)
    examples = read_jsonl(run_dir / "examples.jsonl")
    candidates = read_jsonl(run_dir / "candidates.jsonl")
    query_labels, candidate_labels = load_and_validate_labels(run_dir, examples, candidates)
    report = score_history_records(examples, candidates, query_labels, candidate_labels)
    report["run_id"] = manifest.get("run_id")
    report["tenant_id"] = manifest.get("tenant_id")
    report["scored_at"] = now_rfc3339()
    markdown = render_history_markdown(report)
    write_private_json(run_dir / "report.json", report)
    write_private_bytes(run_dir / "report.md", markdown.encode())
    manifest["scored_at"] = report["scored_at"]
    manifest["files"] = run_file_hashes(run_dir)
    write_private_json(manifest_path, manifest, overwrite=True)
    return report


def render_history_markdown(report: dict[str, Any]) -> str:
    ranking = report["ranking"]
    pre = ranking["pre_rerank"]
    post = ranking["post_rerank"]
    gate = report["answerability_gate"]
    reranker = report["reranker_comparison"]
    outcome = reranker["win_tie_loss"]
    lines = [
        "# Historical chat retrieval evaluation",
        "",
        "> Diagnostic only. This report must not recalibrate the production confidence "
        "gate or become a CI gate.",
        "",
        f"- Run: `{report.get('run_id')}`",
        f"- Examples: {report['examples']}",
        f"- Human-labeled answerable examples: {ranking['evaluated_answerable_queries']}",
        "",
        "## Current-corpus replay",
        "",
        "| Metric | Pre-rerank | Post-rerank | Delta |",
        "|---|---:|---:|---:|",
    ]
    for key, label in (
        ("precision_at_1", "Precision@1"),
        ("precision_at_3", "Precision@3"),
        ("precision_at_5", "Precision@5"),
        ("precision_at_10", "Precision@10"),
        ("judged_pool_recall_at_5", "Judged-pool recall@5"),
        ("judged_pool_recall_at_10", "Judged-pool recall@10"),
        ("mrr", "MRR"),
        ("ndcg_at_10", "nDCG@10"),
    ):
        before = float(pre.get(key, 0.0))
        after = float(post.get(key, 0.0))
        lines.append(f"| {label} | {before:.4f} | {after:.4f} | {after - before:+.4f} |")
    lines.extend(
        [
            "",
            "## Answerability gate",
            "",
            f"- Precision: {float(gate['precision']):.4f}",
            f"- Recall: {float(gate['recall']):.4f}",
            f"- False positives: {gate['false_positive']}",
            f"- False negatives: {gate['false_negative']}",
            "",
            "## Reranker comparison",
            "",
            f"- Win / tie / loss: {outcome['win']} / {outcome['tie']} / {outcome['loss']}",
            f"- Mean relevant-document rank lift: "
            f"{float(reranker['mean_relevant_rank_lift']):+.4f}",
            f"- Harmful demotions: {reranker['harmful_demotions']}",
            "",
            "## Recorded historical evidence",
            "",
            "Recorded metrics cover only stored packed sources. Legacy messages do not "
            "contain the complete pre-rerank pool, so this view is not presented as a "
            "historical reranker evaluation. Schema-v2 messages with complete bounded "
            "ranking traces are reported separately in `report.json`.",
            "",
            "Breakdowns by role, language, scope, feedback, and corpus comparability are "
            "available in `report.json`.",
            "",
        ]
    )
    return "\n".join(lines)


def _verify_immutable_files(run_dir: Path, manifest: dict[str, Any]) -> None:
    hashes = manifest.get("files")
    if not isinstance(hashes, dict):
        raise HistoryEvaluationError("manifest file hashes are missing")
    for name in (
        "examples.jsonl",
        "candidates.jsonl",
        "labels.template.jsonl",
        "ingestion-audit.json",
    ):
        expected = hashes.get(name)
        path = run_dir / name
        if not isinstance(expected, str) or sha256_file(path) != expected:
            raise HistoryEvaluationError(f"{name} does not match the run manifest")
