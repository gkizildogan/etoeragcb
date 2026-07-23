from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from app.evaluation.calibration import sweep_thresholds
from app.evaluation.metrics import grouped_metrics, ranking_metrics
from app.evaluation.schemas import (
    AcceptanceEvaluation,
    CorpusRecord,
    DatasetBundle,
    EvaluationProvenance,
    EvaluationReport,
    GateObservation,
    GoldenQuery,
    ModeEvaluation,
    ModeName,
    QueryRanking,
)
from app.ingest.hashing import SparseVector, sparse_lexical_vector
from app.ingest.normalization import normalize_lexical
from app.rag.candidates import (
    EvidenceCandidate,
    RerankedEvidence,
    stable_reranked_key,
)
from app.rag.context import ContextPacker, TokenCounter
from app.rag.dedup import deduplicate
from app.rag.reranker import Reranker
from app.rag.service import QueryEmbedder

RRF_K = 60
MODES: tuple[ModeName, ...] = (
    "sparse_only",
    "dense_only",
    "hybrid",
    "scoped_hybrid",
    "reranked_hybrid",
)


class EvaluationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dense_limit: int = Field(ge=1, le=500)
    sparse_limit: int = Field(ge=1, le=500)
    rerank_pool: int = Field(ge=1, le=200)
    rerank_keep: int = Field(ge=1, le=100)
    context_token_budget: int = Field(ge=256, le=7_000)
    section_limit: int = Field(ge=1, le=20)
    source_limit: int = Field(ge=1, le=50)
    domain_limit: int = Field(ge=1, le=20)
    web_limit: int = Field(ge=1, le=20)
    report_limit: int = Field(default=10, ge=10, le=100)


@dataclass(frozen=True, slots=True)
class _BranchRanking:
    ids: tuple[str, ...]
    scores: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class _QueryVectors:
    dense: list[float]
    sparse: SparseVector
    dense_latency_ms: float


async def evaluate(
    dataset: DatasetBundle,
    *,
    embedder: QueryEmbedder,
    reranker: Reranker,
    token_counter: TokenCounter,
    config: EvaluationConfig,
    provenance: EvaluationProvenance,
) -> EvaluationReport:
    corpus_embeddings = await _embed_corpus(dataset.corpus, embedder)
    corpus_sparse = {
        record.id: sparse_lexical_vector(normalize_lexical(record.text))
        for record in dataset.corpus
    }
    packer = ContextPacker(
        token_counter,
        token_budget=config.context_token_budget,
        max_candidates=config.rerank_keep,
        section_limit=config.section_limit,
        source_limit=config.source_limit,
        domain_limit=config.domain_limit,
        web_limit=config.web_limit,
    )
    rankings: dict[ModeName, list[QueryRanking]] = {mode: [] for mode in MODES}
    observations: list[GateObservation] = []
    for query in dataset.queries:
        vectors = await _query_vectors(query, embedder)
        global_records = _available_records(dataset.corpus, query, scoped=False)
        scoped_records = _available_records(dataset.corpus, query, scoped=True)

        sparse_started = time.perf_counter()
        sparse = _sparse_ranking(
            global_records,
            vectors.sparse,
            corpus_sparse,
            config.sparse_limit,
        )
        sparse_latency = _elapsed_ms(sparse_started)
        _append_ranking(
            rankings["sparse_only"],
            query,
            sparse,
            sparse_latency,
            config.report_limit,
        )

        dense_started = time.perf_counter()
        dense = _dense_ranking(
            global_records,
            vectors.dense,
            corpus_embeddings,
            config.dense_limit,
        )
        dense_latency = vectors.dense_latency_ms + _elapsed_ms(dense_started)
        _append_ranking(
            rankings["dense_only"],
            query,
            dense,
            dense_latency,
            config.report_limit,
        )

        hybrid_started = time.perf_counter()
        hybrid = _hybrid_ranking(
            global_records,
            query,
            vectors,
            corpus_embeddings,
            corpus_sparse,
            config,
        )
        hybrid_latency = vectors.dense_latency_ms + _elapsed_ms(hybrid_started)
        _append_ranking(
            rankings["hybrid"],
            query,
            hybrid,
            hybrid_latency,
            config.report_limit,
        )

        scoped_started = time.perf_counter()
        scoped = _hybrid_ranking(
            scoped_records,
            query,
            vectors,
            corpus_embeddings,
            corpus_sparse,
            config,
        )
        scoped_latency = vectors.dense_latency_ms + _elapsed_ms(scoped_started)
        _append_ranking(
            rankings["scoped_hybrid"],
            query,
            scoped,
            scoped_latency,
            config.report_limit,
        )

        rerank_started = time.perf_counter()
        candidates = _evidence_candidates(query, scoped, scoped_records, config.rerank_pool)
        reranked = await reranker.rerank(query.query, candidates)
        deduplicated = deduplicate(reranked)
        context = await packer.pack(deduplicated.candidates)
        packed = tuple(source.evidence for source in context.sources)
        reranked_ids = tuple(item.candidate.candidate_id for item in reranked)
        reranked_scores = tuple(item.rerank_score for item in reranked)
        reranked_latency = scoped_latency + _elapsed_ms(rerank_started)
        reranked_ranking = _BranchRanking(ids=reranked_ids, scores=reranked_scores)
        _append_ranking(
            rankings["reranked_hybrid"],
            query,
            reranked_ranking,
            reranked_latency,
            config.report_limit,
        )
        observations.append(_gate_observation(query, packed))

    modes = tuple(
        _mode_evaluation(
            mode,
            dataset,
            tuple(rankings[mode]),
        )
        for mode in MODES
    )
    calibration = sweep_thresholds(
        tuple(observations),
        precision_target=dataset.manifest.gate_targets.precision_min,
        recall_target=dataset.manifest.gate_targets.recall_min,
    )
    mode_by_name = {item.mode: item for item in modes}
    targets = dataset.manifest.ranking_targets
    checks = {
        "reranked_recall_at_5": (
            mode_by_name["reranked_hybrid"].metrics.recall_at_5 >= targets.reranked_recall_at_5_min
        ),
        "reranked_mrr": (mode_by_name["reranked_hybrid"].metrics.mrr >= targets.reranked_mrr_min),
        "reranked_ndcg_at_10": (
            mode_by_name["reranked_hybrid"].metrics.ndcg_at_10 >= targets.reranked_ndcg_at_10_min
        ),
        "scoped_recall_at_5": (
            mode_by_name["scoped_hybrid"].metrics.recall_at_5 >= targets.scoped_recall_at_5_min
        ),
        "gate_precision": (
            calibration.metrics.precision >= dataset.manifest.gate_targets.precision_min
        ),
        "gate_recall": (calibration.metrics.recall >= dataset.manifest.gate_targets.recall_min),
    }
    return EvaluationReport(
        schema_version=1,
        report_id="p10-retrieval-evaluation-v1",
        provenance=provenance,
        configuration=config.model_dump(),
        modes=modes,
        calibration=calibration,
        acceptance=AcceptanceEvaluation(
            passed=all(checks.values()),
            checks=checks,
        ),
    )


async def _embed_corpus(
    corpus: tuple[CorpusRecord, ...],
    embedder: QueryEmbedder,
) -> dict[str, list[float]]:
    result: dict[str, list[float]] = {}
    batch_size = 16
    for offset in range(0, len(corpus), batch_size):
        batch = corpus[offset : offset + batch_size]
        vectors = await embedder.embed([item.text for item in batch])
        if len(vectors) != len(batch):
            raise RuntimeError("corpus embedding count mismatch")
        result.update({record.id: vector for record, vector in zip(batch, vectors, strict=True)})
    return result


async def _query_vectors(
    query: GoldenQuery,
    embedder: QueryEmbedder,
) -> _QueryVectors:
    started = time.perf_counter()
    vectors = await embedder.embed([query.query])
    latency = _elapsed_ms(started)
    if len(vectors) != 1:
        raise RuntimeError("query embedding count mismatch")
    return _QueryVectors(
        dense=vectors[0],
        sparse=sparse_lexical_vector(normalize_lexical(query.query)),
        dense_latency_ms=latency,
    )


def _available_records(
    corpus: tuple[CorpusRecord, ...],
    query: GoldenQuery,
    *,
    scoped: bool,
) -> tuple[CorpusRecord, ...]:
    records = tuple(
        record for record in corpus if record.source_type == "document" or query.web_search
    )
    if not scoped:
        return records
    if query.scope.document_ids:
        allowed = set(query.scope.document_ids)
        records = tuple(record for record in records if record.document_id in allowed)
    if query.scope.collection_ids:
        allowed_collections = set(query.scope.collection_ids)
        records = tuple(
            record for record in records if allowed_collections.intersection(record.collection_ids)
        )
    if query.scope.headings:
        headings = {normalize_lexical(value) for value in query.scope.headings}
        records = tuple(
            record for record in records if normalize_lexical(record.heading) in headings
        )
    return records


def _sparse_ranking(
    records: tuple[CorpusRecord, ...],
    query: SparseVector,
    corpus_vectors: dict[str, SparseVector],
    limit: int,
) -> _BranchRanking:
    scores = {
        record.id: score
        for record in records
        if (score := _sparse_dot(query, corpus_vectors[record.id])) > 0
    }
    return _rank_scores(scores, limit)


def _dense_ranking(
    records: tuple[CorpusRecord, ...],
    query: list[float],
    corpus_vectors: dict[str, list[float]],
    limit: int,
) -> _BranchRanking:
    scores = {record.id: _cosine(query, corpus_vectors[record.id]) for record in records}
    return _rank_scores(scores, limit)


def _hybrid_ranking(
    records: tuple[CorpusRecord, ...],
    query: GoldenQuery,
    vectors: _QueryVectors,
    corpus_embeddings: dict[str, list[float]],
    corpus_sparse: dict[str, SparseVector],
    config: EvaluationConfig,
) -> _BranchRanking:
    dense = _dense_ranking(records, vectors.dense, corpus_embeddings, config.dense_limit)
    sparse = _sparse_ranking(records, vectors.sparse, corpus_sparse, config.sparse_limit)
    fusion: dict[str, float] = {}
    for ranking in (dense, sparse):
        for rank, candidate_id in enumerate(ranking.ids, start=1):
            fusion[candidate_id] = fusion.get(candidate_id, 0.0) + 1 / (RRF_K + rank)
    base_order = tuple(
        candidate_id
        for candidate_id, _score in sorted(
            fusion.items(),
            key=lambda item: (-item[1], item[0]),
        )
    )
    record_by_id = {record.id: record for record in records}
    normalized_terms = tuple(
        (term, normalize_lexical(term)) for term in query.exact_terms if normalize_lexical(term)
    )
    exact_matches: list[tuple[str, int, int, int]] = []
    hint_matches: list[tuple[str, int]] = []
    for base_rank, candidate_id in enumerate(base_order, start=1):
        record = record_by_id[candidate_id]
        lexical = normalize_lexical(record.text)
        matches = [term for term, normalized in normalized_terms if normalized in lexical]
        if matches:
            position = min(
                lexical.find(normalized)
                for _term, normalized in normalized_terms
                if normalized in lexical
            )
            exact_matches.append((candidate_id, len(matches), position, base_rank))
        if record.document_id in query.boost_document_ids:
            hint_matches.append((candidate_id, base_rank))
    exact_matches.sort(key=lambda item: (-item[1], item[2], item[3], item[0]))
    for rank, (candidate_id, _count, _position, _base_rank) in enumerate(
        exact_matches,
        start=1,
    ):
        fusion[candidate_id] += 1 / (RRF_K + rank)
    hint_matches.sort(key=lambda item: (item[1], item[0]))
    for rank, (candidate_id, _base_rank) in enumerate(hint_matches, start=1):
        fusion[candidate_id] += 1 / (RRF_K + rank)
    return _rank_scores(fusion, config.rerank_pool)


def _evidence_candidates(
    query: GoldenQuery,
    ranking: _BranchRanking,
    records: tuple[CorpusRecord, ...],
    limit: int,
) -> tuple[EvidenceCandidate, ...]:
    record_by_id = {item.id: item for item in records}
    normalized_terms = tuple(
        (term, normalize_lexical(term)) for term in query.exact_terms if normalize_lexical(term)
    )
    candidates: list[EvidenceCandidate] = []
    for rank, (candidate_id, score) in enumerate(
        zip(ranking.ids[:limit], ranking.scores[:limit], strict=True),
        start=1,
    ):
        record = record_by_id[candidate_id]
        lexical = normalize_lexical(record.text)
        exact_terms = tuple(term for term, normalized in normalized_terms if normalized in lexical)
        hints = (
            ("ambiguous_document_hint",) if record.document_id in query.boost_document_ids else ()
        )
        uri = str(record.uri) if record.uri is not None else None
        candidates.append(
            EvidenceCandidate(
                candidate_id=record.id,
                source_type=record.source_type,
                source_key=record.document_id,
                section_key=f"{record.document_id}:{normalize_lexical(record.heading)}",
                domain=record.domain,
                title=record.document_title,
                source_filename=record.source_filename,
                uri=uri,
                page_start=record.page,
                page_end=record.page,
                char_start=0,
                char_end=len(record.text),
                text_original=record.text,
                text_lexical=lexical,
                content_sha256=hashlib.sha256(record.text.encode()).hexdigest(),
                lexical_sha256=hashlib.sha256(lexical.encode()).hexdigest(),
                retrieval_rank=rank,
                retrieval_score=score,
                matched_exact_terms=exact_terms,
                matched_hints=hints,
                provenance={
                    "evaluation_document_id": record.document_id,
                    "evaluation_heading": record.heading,
                    "duplicate_group": record.duplicate_group,
                },
            )
        )
    return tuple(candidates)


def _gate_observation(
    query: GoldenQuery,
    packed: tuple[RerankedEvidence, ...],
) -> GateObservation:
    ranked = tuple(sorted(packed, key=stable_reranked_key))
    top_score = ranked[0].rerank_score if ranked else None
    second_score = ranked[1].rerank_score if len(ranked) > 1 else None
    exact_scores = [item.rerank_score for item in ranked if item.candidate.matched_exact_terms]
    relevant_in_context = any(item.candidate.candidate_id in query.relevance for item in ranked)
    return GateObservation(
        query_id=query.id,
        expected_answer=query.answerable and relevant_in_context,
        answerable=query.answerable,
        relevant_in_context=relevant_in_context,
        top_score=top_score,
        second_score=second_score,
        score_margin=(
            top_score - second_score
            if top_score is not None and second_score is not None
            else top_score
        ),
        best_exact_score=max(exact_scores) if exact_scores else None,
        evidence_count=len(ranked),
    )


def _mode_evaluation(
    mode: ModeName,
    dataset: DatasetBundle,
    rankings: tuple[QueryRanking, ...],
) -> ModeEvaluation:
    return ModeEvaluation(
        mode=mode,
        metrics=ranking_metrics(dataset.queries, rankings, dataset.corpus),
        by_language=grouped_metrics(
            dataset.queries,
            rankings,
            dataset.corpus,
            key=lambda query: query.language,
        ),
        by_category=grouped_metrics(
            dataset.queries,
            rankings,
            dataset.corpus,
            key=lambda query: query.category,
        ),
        queries=rankings,
    )


def _append_ranking(
    target: list[QueryRanking],
    query: GoldenQuery,
    ranking: _BranchRanking,
    latency_ms: float,
    limit: int,
) -> None:
    target.append(
        QueryRanking(
            query_id=query.id,
            ranked_ids=ranking.ids[:limit],
            scores=tuple(round(value, 8) for value in ranking.scores[:limit]),
            latency_ms=latency_ms,
        )
    )


def _rank_scores(scores: dict[str, float], limit: int) -> _BranchRanking:
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return _BranchRanking(
        ids=tuple(item[0] for item in ordered),
        scores=tuple(item[1] for item in ordered),
    )


def _sparse_dot(left: SparseVector, right: SparseVector) -> float:
    left_values = dict(zip(left.indices, left.values, strict=True))
    return sum(
        left_values.get(index, 0.0) * value
        for index, value in zip(right.indices, right.values, strict=True)
    )


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("dense vector dimensions differ")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1_000
