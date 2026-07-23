from __future__ import annotations

import argparse
import asyncio
import uuid
from pathlib import Path

import httpx

from app.config import get_settings
from app.core.db import create_database_engine, create_session_factory
from app.evaluation.dataset import evaluator_sha256, load_dataset
from app.evaluation.engine import EvaluationConfig, evaluate
from app.evaluation.feedback import feedback_records, write_feedback_jsonl
from app.evaluation.report import (
    verify_report,
    write_gate_configuration,
    write_report,
)
from app.evaluation.schemas import EvaluationProvenance
from app.ingest.embedder import TeiClient
from app.rag.context import VllmTokenCounter
from app.rag.reranker import TeiReranker

DEFAULT_DATASET = Path("evaluation/golden/v1")
DEFAULT_JSON_REPORT = Path("evaluation/reports/p10-retrieval-v1.json")
DEFAULT_MARKDOWN_REPORT = Path("evaluation/reports/p10-retrieval-v1.md")
DEFAULT_GATE = Path("app/rag/calibration/retrieval_gate.v1.json")
PACKAGE_ROOT = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reproducible retrieval evaluation and calibration"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run live model evaluation")
    run.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    run.add_argument("--json-report", type=Path, default=DEFAULT_JSON_REPORT)
    run.add_argument("--markdown-report", type=Path, default=DEFAULT_MARKDOWN_REPORT)
    run.add_argument("--gate", type=Path, default=DEFAULT_GATE)
    run.add_argument(
        "--no-write-gate",
        action="store_true",
        help="write reports but do not replace the calibrated gate artifact",
    )
    verify = subparsers.add_parser("verify", help="verify committed report provenance and gates")
    verify.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    verify.add_argument("--report", type=Path, default=DEFAULT_JSON_REPORT)
    verify.add_argument("--gate", type=Path, default=DEFAULT_GATE)
    feedback = subparsers.add_parser(
        "export-feedback",
        help="export one tenant's feedback to a mode-0600 JSONL file",
    )
    feedback.add_argument("--tenant-id", type=uuid.UUID, required=True)
    feedback.add_argument("--output", type=Path, required=True)
    feedback.add_argument("--include-content", action="store_true")
    feedback.add_argument("--overwrite", action="store_true")
    return parser


async def run_evaluation(args: argparse.Namespace) -> int:
    settings = get_settings()
    dataset = load_dataset(args.dataset)
    provenance = EvaluationProvenance(
        dataset_name=dataset.manifest.name,
        dataset_version=dataset.manifest.version,
        dataset_sha256=dataset.manifest.dataset_sha256,
        query_examples=len(dataset.queries),
        corpus_records=len(dataset.corpus),
        evaluator_sha256=evaluator_sha256(PACKAGE_ROOT),
        embedding_model=settings.embed_model,
        embedding_revision=settings.embed_revision,
        reranker_model=settings.rerank_model,
        reranker_revision=settings.rerank_revision,
    )
    config = EvaluationConfig(
        dense_limit=settings.retrieve_dense_n,
        sparse_limit=settings.retrieve_sparse_n,
        rerank_pool=settings.rerank_pool_n,
        rerank_keep=settings.rerank_keep,
        context_token_budget=settings.context_token_budget,
        section_limit=settings.section_chunk_limit,
        source_limit=settings.document_chunk_limit,
        domain_limit=settings.domain_chunk_limit,
        web_limit=settings.web_context_limit,
    )
    async with httpx.AsyncClient(
        base_url=str(settings.embed_url).rstrip("/"),
        timeout=60.0,
    ) as embedding_http:
        embedder = TeiClient(
            str(settings.embed_url),
            expected_dimension=settings.embed_dim,
            client=embedding_http,
        )
        reranker = TeiReranker(
            str(settings.rerank_url),
            model_revision=settings.rerank_revision,
            max_candidates=settings.rerank_pool_n,
            cache_ttl=0,
        )
        token_counter = VllmTokenCounter(
            str(settings.vllm_base_url),
            settings.vllm_model,
        )
        try:
            report = await evaluate(
                dataset,
                embedder=embedder,
                reranker=reranker,
                token_counter=token_counter,
                config=config,
                provenance=provenance,
            )
        finally:
            await reranker.close()
            await token_counter.close()
    write_report(
        report,
        json_path=args.json_report,
        markdown_path=args.markdown_report,
    )
    if report.acceptance.passed and not args.no_write_gate:
        write_gate_configuration(report, path=args.gate)
    print(args.markdown_report.read_text(encoding="utf-8"))
    return 0 if report.acceptance.passed else 1


async def export_feedback(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_database_engine(settings.resolved_database_url())
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            records = await feedback_records(
                session,
                tenant_id=args.tenant_id,
                include_content=bool(args.include_content),
            )
        write_feedback_jsonl(
            records,
            path=args.output,
            overwrite=bool(args.overwrite),
        )
    finally:
        await engine.dispose()
    print(f"Exported {len(records)} feedback records to {args.output}")
    return 0


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "run":
        raise SystemExit(asyncio.run(run_evaluation(args)))
    if args.command == "export-feedback":
        raise SystemExit(asyncio.run(export_feedback(args)))
    report = verify_report(
        args.report,
        dataset_root=args.dataset,
        package_root=PACKAGE_ROOT,
        gate_path=args.gate,
    )
    print(
        f"PASS: {report.report_id} matches dataset, evaluator, model, gate, "
        "and acceptance provenance"
    )


if __name__ == "__main__":
    main()
