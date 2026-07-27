from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import false, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.evaluation.dataset import evaluator_sha256
from app.evaluation.history import (
    HistoricalTurn,
    HistoryEvaluationError,
    create_private_run_directory,
    historical_turns,
    now_rfc3339,
    run_file_hashes,
    write_private_bytes,
    write_private_json,
    write_private_jsonl,
)
from app.evaluation.history_audit import QdrantPointReader, audit_ingestion
from app.ingest.embedder import TeiClient
from app.models import Chunk, Tenant
from app.rag.combined import CombinedRetrievalResult, CombinedRetrievalService
from app.rag.context import ContextPacker, VllmTokenCounter
from app.rag.gate import ConfidenceGate, load_gate_artifact
from app.rag.planner import VllmPlanner
from app.rag.postprocess import PostRetrievalService
from app.rag.reranker import TeiReranker
from app.rag.retriever import HybridRetriever, QdrantHybridSearch
from app.rag.scope import MetadataResolver
from app.rag.service import RetrievalService
from app.rag.web import WebRetrievalResult

PACKAGE_ROOT = Path(__file__).resolve().parent


class DisabledWebRetriever:
    async def retrieve(self, query: str) -> WebRetrievalResult:
        del query
        return WebRetrievalResult(status="disabled")


@dataclass(slots=True)
class HistoryReplayRuntime:
    retrieval: CombinedRetrievalService
    tokenizer: TeiClient
    point_reader: QdrantPointReader
    resources: tuple[Any, ...]

    async def close(self) -> None:
        for resource in reversed(self.resources):
            close = getattr(resource, "close", None)
            if close is not None:
                await close()


def build_history_replay_runtime(settings: Settings) -> HistoryReplayRuntime:
    qdrant = QdrantHybridSearch(str(settings.qdrant_url), settings.qdrant_collection)
    tokenizer = TeiClient(str(settings.embed_url), expected_dimension=settings.embed_dim)
    documents = RetrievalService(
        VllmPlanner(str(settings.vllm_base_url), settings.vllm_model),
        MetadataResolver(),
        tokenizer,
        HybridRetriever(
            qdrant,
            dense_limit=settings.retrieve_dense_n,
            sparse_limit=settings.retrieve_sparse_n,
            pool_limit=settings.rerank_pool_n,
            section_chunk_limit=settings.section_chunk_limit,
            neighbor_radius=settings.section_neighbor_radius,
        ),
        cache=None,
        plan_cache_ttl=0,
        retrieval_cache_ttl=0,
        planner_revision=settings.vllm_model_revision,
        embedding_revision=settings.embed_revision,
    )
    reranker = TeiReranker(
        str(settings.rerank_url),
        model_revision=settings.rerank_revision,
        max_candidates=settings.rerank_pool_n,
        cache=None,
        cache_ttl=0,
    )
    token_counter = VllmTokenCounter(str(settings.vllm_base_url), settings.vllm_model)
    gate_path = settings.retrieval_gate_config
    if not gate_path.is_absolute() and not gate_path.exists():
        gate_path = Path(__file__).resolve().parents[2] / gate_path
    gate = ConfidenceGate(
        load_gate_artifact(gate_path),
        embedding_model=settings.embed_model,
        embedding_revision=settings.embed_revision,
        reranker_model=settings.rerank_model,
        reranker_revision=settings.rerank_revision,
    )
    post = PostRetrievalService(
        reranker,
        ContextPacker(
            token_counter,
            token_budget=settings.context_token_budget,
            max_candidates=settings.rerank_keep,
            section_limit=settings.section_chunk_limit,
            source_limit=settings.document_chunk_limit,
            domain_limit=settings.domain_chunk_limit,
            web_limit=settings.web_context_limit,
        ),
        gate,
    )
    retrieval = CombinedRetrievalService(
        documents,
        DisabledWebRetriever(),
        post,
        pool_limit=settings.rerank_pool_n,
    )
    point_reader = QdrantPointReader(str(settings.qdrant_url), settings.qdrant_collection)
    return HistoryReplayRuntime(
        retrieval=retrieval,
        tokenizer=tokenizer,
        point_reader=point_reader,
        resources=(qdrant, reranker, token_counter, point_reader),
    )


async def prepare_history_run(
    factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    tenant_id: uuid.UUID,
    output: Path,
    include_private_content: bool,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    runtime: HistoryReplayRuntime | None = None,
) -> dict[str, Any]:
    if not include_private_content:
        raise HistoryEvaluationError(
            "historical evaluation contains private chat and document content; "
            "pass --include-private-content"
        )
    if start_at is not None and end_at is not None and start_at >= end_at:
        raise HistoryEvaluationError("start time must precede end time")
    create_private_run_directory(output)
    owned_runtime = runtime is None
    replay_runtime = runtime or build_history_replay_runtime(settings)
    try:
        async with factory() as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
            if tenant is None:
                raise HistoryEvaluationError("tenant does not exist")
            turns = await historical_turns(
                session,
                tenant_id=tenant_id,
                start_at=start_at,
                end_at=end_at,
            )
            examples, candidates = await _replay_turns(
                session,
                tenant_id=tenant_id,
                turns=turns,
                retrieval=replay_runtime.retrieval,
            )
            audit = await audit_ingestion(
                session,
                tenant_id=tenant_id,
                turns=turns,
                settings=settings,
                tokenizer=replay_runtime.tokenizer,
                point_reader=replay_runtime.point_reader,
            )
        labels = _label_template(examples, candidates)
        write_private_jsonl(output / "examples.jsonl", examples)
        write_private_jsonl(output / "candidates.jsonl", candidates)
        write_private_jsonl(output / "labels.template.jsonl", labels)
        template_bytes = (output / "labels.template.jsonl").read_bytes()
        write_private_bytes(output / "labels.jsonl", template_bytes)
        write_private_json(output / "ingestion-audit.json", audit)
        manifest = {
            "schema_version": 1,
            "run_id": output.name,
            "tenant_id": str(tenant_id),
            "created_at": now_rfc3339(),
            "time_range": {
                "start": start_at.isoformat() if start_at is not None else None,
                "end": end_at.isoformat() if end_at is not None else None,
            },
            "current_generation_id": tenant.active_index_generation_id,
            "current_retrieval_revision": tenant.retrieval_revision,
            "models": {
                "planner_model": settings.vllm_model,
                "planner_revision": settings.vllm_model_revision,
                "embedding_model": settings.embed_model,
                "embedding_revision": settings.embed_revision,
                "reranker_model": settings.rerank_model,
                "reranker_revision": settings.rerank_revision,
            },
            "configuration_sha256": _configuration_sha256(settings),
            "evaluator_sha256": evaluator_sha256(PACKAGE_ROOT),
            "counts": {
                "examples": len(examples),
                "candidates": len(candidates),
                "recorded_candidates": sum(
                    item["recorded_rank"] is not None for item in candidates
                ),
                "current_candidates": sum(
                    item["current_retrieval_rank"] is not None for item in candidates
                ),
            },
            "files": run_file_hashes(output),
            "privacy": {
                "contains_private_content": True,
                "directory_mode": "0700",
                "file_mode": "0600",
            },
        }
        write_private_json(output / "manifest.json", manifest)
        if audit["summary"]["failed_active_versions"]:
            raise HistoryEvaluationError(
                "active-version ingestion audit failed: "
                + ", ".join(audit["summary"]["failed_active_versions"])
            )
        return manifest
    except Exception:
        # Preserve a completed audit/run for diagnosis; the directory is never overwritten.
        raise
    finally:
        if owned_runtime:
            await replay_runtime.close()


async def _replay_turns(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    turns: tuple[HistoricalTurn, ...],
    retrieval: CombinedRetrievalService,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    examples: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for turn in turns:
        replay: CombinedRetrievalResult | None = None
        replay_error: str | None = None
        try:
            replay = await retrieval.retrieve(
                session,
                tenant_id=tenant_id,
                message=turn.query,
                web_search=False,
                explicit_document_ids=turn.document_ids,
                explicit_collection_ids=turn.collection_ids,
            )
        except Exception as exc:
            replay_error = type(exc).__name__
        current_rows = _current_candidate_rows(turn, replay) if replay is not None else []
        recorded_rows = await _recorded_candidate_rows(session, tenant_id, turn)
        pooled = _pool_candidates(recorded_rows, current_rows)
        comparability = _corpus_comparability(turn, replay, pooled)
        examples.append(
            {
                "schema_version": 1,
                "example_id": str(turn.example_id),
                "created_at": turn.created_at.isoformat(),
                "query": turn.query,
                "answer": turn.answer,
                "document_ids": [str(item) for item in turn.document_ids],
                "collection_ids": [str(item) for item in turn.collection_ids],
                "scope_type": _scope_type(turn),
                "role_group": turn.role_group,
                "role_basis": turn.role_basis,
                "feedback_rating": turn.feedback_rating,
                "feedback_comment": turn.feedback_comment,
                "original_route": turn.original_route,
                "original_gate": turn.original_gate,
                "recorded_pool_complete": turn.recorded_retrieval_schema_version == 2,
                "requested_web_search": turn.requested_web_search,
                "replay_web_status": "disabled",
                "current_route": (replay.post_retrieval.gate.route if replay is not None else None),
                "current_gate_route": (
                    replay.post_retrieval.gate.route if replay is not None else None
                ),
                "current_gate": (
                    replay.post_retrieval.gate.model_dump(mode="json")
                    if replay is not None
                    else None
                ),
                "current_generation_id": (
                    replay.documents.scope.generation_id if replay is not None else None
                ),
                "current_retrieval_revision": (
                    replay.documents.scope.retrieval_revision if replay is not None else None
                ),
                "corpus_comparability": comparability,
                "replay_error": replay_error,
            }
        )
        candidates.extend(pooled)
    return examples, candidates


def _current_candidate_rows(
    turn: HistoricalTurn, replay: CombinedRetrievalResult
) -> list[dict[str, Any]]:
    reranked = {item.candidate.candidate_id: item for item in replay.post_retrieval.reranked}
    packed = {
        source.evidence.candidate.candidate_id for source in replay.post_retrieval.context.sources
    }
    retained = {
        item.candidate.candidate_id for item in replay.post_retrieval.deduplication.candidates
    }
    result: list[dict[str, Any]] = []
    for candidate in replay.combined_pool:
        if candidate.source_type != "document":
            continue
        provenance = candidate.provenance
        version_id = provenance.get("document_version_id")
        current = reranked.get(candidate.candidate_id)
        if not isinstance(version_id, str):
            continue
        result.append(
            {
                "schema_version": 1,
                "example_id": str(turn.example_id),
                "candidate_key": f"document:{version_id}:{candidate.candidate_id}",
                "source_type": "document",
                "candidate_id": candidate.candidate_id,
                "text": candidate.text_original,
                "title": candidate.title,
                "source_filename": candidate.source_filename,
                "content_sha256": candidate.content_sha256,
                "document_id": provenance.get("document_id"),
                "document_version_id": version_id,
                "section_id": provenance.get("section_id"),
                "chunk_index": provenance.get("chunk_index"),
                "page_start": candidate.page_start,
                "page_end": candidate.page_end,
                "recorded_rank": None,
                "recorded_packed": False,
                "recorded_rerank_rank": None,
                "current_retrieval_rank": candidate.retrieval_rank,
                "current_dense_rank": provenance.get("dense_rank"),
                "current_sparse_rank": provenance.get("sparse_rank"),
                "current_branch_rank": provenance.get("branch_retrieval_rank"),
                "current_rerank_rank": (current.rerank_rank if current is not None else None),
                "current_rerank_score": (current.rerank_score if current is not None else None),
                "dedup_retained": candidate.candidate_id in retained,
                "context_packed": candidate.candidate_id in packed,
                "availability": "available",
            }
        )
    return result


async def _recorded_candidate_rows(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    turn: HistoricalTurn,
) -> list[dict[str, Any]]:
    recorded_items = turn.recorded_candidates if turn.recorded_candidates else turn.recorded_sources
    packed_by_id = {
        str(source.get("candidate_id")): source
        for source in turn.recorded_sources
        if isinstance(source.get("candidate_id"), str)
    }
    chunk_ids: list[uuid.UUID] = []
    for source in recorded_items:
        raw = source.get("candidate_id")
        try:
            if isinstance(raw, str):
                chunk_ids.append(uuid.UUID(raw))
        except ValueError:
            continue
    chunks = {
        item.id: item
        for item in await session.scalars(
            select(Chunk).where(
                Chunk.tenant_id == tenant_id,
                Chunk.id.in_(chunk_ids) if chunk_ids else false(),
            )
        )
    }
    rows: list[dict[str, Any]] = []
    for fallback_rank, source in enumerate(recorded_items, start=1):
        candidate_id = source.get("candidate_id")
        packed_source = packed_by_id.get(str(candidate_id), {})
        provenance = source.get("provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        packed_provenance = packed_source.get("provenance")
        packed_provenance = packed_provenance if isinstance(packed_provenance, dict) else {}
        version_id = provenance.get(
            "document_version_id", packed_provenance.get("document_version_id")
        )
        source_type = source.get("source_type")
        if not isinstance(candidate_id, str):
            continue
        chunk: Chunk | None = None
        try:
            chunk = chunks.get(uuid.UUID(candidate_id))
        except ValueError:
            pass
        if source_type == "document" and isinstance(version_id, str):
            candidate_key = f"document:{version_id}:{candidate_id}"
        else:
            candidate_key = (
                "web:"
                + hashlib.sha256(
                    f"{source.get('content_sha256')}:{candidate_id}".encode()
                ).hexdigest()
            )
        rows.append(
            {
                "schema_version": 1,
                "example_id": str(turn.example_id),
                "candidate_key": candidate_key,
                "source_type": source_type if source_type in {"document", "web"} else "document",
                "candidate_id": candidate_id,
                "text": chunk.text_original if chunk is not None else None,
                "title": None,
                "source_filename": None,
                "content_sha256": source.get("content_sha256"),
                "document_id": provenance.get("document_id", packed_provenance.get("document_id")),
                "document_version_id": version_id,
                "section_id": provenance.get("section_id", packed_provenance.get("section_id")),
                "chunk_index": provenance.get("chunk_index", packed_provenance.get("chunk_index")),
                "page_start": chunk.page_start if chunk is not None else None,
                "page_end": chunk.page_end if chunk is not None else None,
                "recorded_rank": source.get("retrieval_rank", fallback_rank),
                "recorded_packed": (
                    bool(source.get("context_packed")) if turn.recorded_candidates else True
                ),
                "recorded_rerank_rank": source.get("rerank_rank", packed_source.get("rerank_rank")),
                "current_retrieval_rank": None,
                "current_dense_rank": None,
                "current_sparse_rank": None,
                "current_branch_rank": None,
                "current_rerank_rank": None,
                "current_rerank_score": None,
                "dedup_retained": None,
                "context_packed": None,
                "availability": ("available" if chunk is not None else "unavailable_by_retention"),
            }
        )
    return rows


def _pool_candidates(
    recorded: list[dict[str, Any]], current: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    pooled: dict[str, dict[str, Any]] = {}
    for item in [*recorded, *current]:
        key = str(item["candidate_key"])
        existing = pooled.get(key)
        if existing is None:
            pooled[key] = item
            continue
        merged = dict(existing)
        for field, value in item.items():
            if value is not None and (
                merged.get(field) is None
                or field
                in {
                    "current_retrieval_rank",
                    "current_dense_rank",
                    "current_sparse_rank",
                    "current_branch_rank",
                    "current_rerank_rank",
                    "current_rerank_score",
                    "recorded_rerank_rank",
                    "dedup_retained",
                    "context_packed",
                }
            ):
                merged[field] = value
        merged["recorded_packed"] = bool(
            existing.get("recorded_packed") or item.get("recorded_packed")
        )
        pooled[key] = merged
    return sorted(
        pooled.values(),
        key=lambda item: (
            item["current_retrieval_rank"] is None,
            item["current_retrieval_rank"] or 10**9,
            item["recorded_rank"] or 10**9,
            item["candidate_key"],
        ),
    )


def _corpus_comparability(
    turn: HistoricalTurn,
    replay: CombinedRetrievalResult | None,
    candidates: list[dict[str, Any]],
) -> str:
    if any(item["availability"] == "unavailable_by_retention" for item in candidates):
        return "unavailable_by_retention"
    if replay is None:
        return "unknown"
    recorded_versions = {
        item.get("document_version_id")
        for item in candidates
        if item["recorded_rank"] is not None and item.get("document_version_id") is not None
    }
    current_versions = {str(item) for item in replay.documents.scope.version_ids}
    if turn.recorded_retrieval_schema_version is None:
        return "legacy_unknown"
    return "unchanged" if recorded_versions.issubset(current_versions) else "changed"


def _scope_type(turn: HistoricalTurn) -> str:
    if turn.document_ids and turn.collection_ids:
        return "document_and_collection"
    if turn.document_ids:
        return "document"
    if turn.collection_ids:
        return "collection"
    return "unscoped"


def _label_template(
    examples: list[dict[str, Any]], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for example in examples:
        records.append(
            {
                "schema_version": 1,
                "kind": "query",
                "example_id": example["example_id"],
                "answerable": None,
                "language": None,
                "label_source": "human",
                "reviewer": "",
                "reviewed_at": None,
                "notes": None,
            }
        )
    for candidate in candidates:
        records.append(
            {
                "schema_version": 1,
                "kind": "candidate",
                "example_id": candidate["example_id"],
                "candidate_key": candidate["candidate_key"],
                "relevance": None,
                "label_source": "human",
                "reviewer": "",
                "reviewed_at": None,
                "notes": None,
            }
        )
    return records


def _configuration_sha256(settings: Settings) -> str:
    value = {
        "retrieve_dense_n": settings.retrieve_dense_n,
        "retrieve_sparse_n": settings.retrieve_sparse_n,
        "rerank_pool_n": settings.rerank_pool_n,
        "rerank_keep": settings.rerank_keep,
        "context_token_budget": settings.context_token_budget,
        "section_chunk_limit": settings.section_chunk_limit,
        "document_chunk_limit": settings.document_chunk_limit,
        "domain_chunk_limit": settings.domain_chunk_limit,
    }
    import orjson

    return hashlib.sha256(orjson.dumps(value, option=orjson.OPT_SORT_KEYS)).hexdigest()
