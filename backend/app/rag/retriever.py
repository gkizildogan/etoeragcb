from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict
from qdrant_client import AsyncQdrantClient, models
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.hashing import SparseVector as LexicalSparseVector
from app.ingest.normalization import normalize_lexical
from app.models import Chunk, Document, Section
from app.rag.scope import HintBoost, ResolvedScope

RRF_K = 60


class BranchHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: uuid.UUID
    score: float


class BranchResults(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dense: tuple[BranchHit, ...]
    sparse: tuple[BranchHit, ...]


class RetrievalCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: uuid.UUID
    tenant_id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    section_id: uuid.UUID
    document_title: str
    source_filename: str
    section_path_original: str
    page_start: int
    page_end: int
    char_start: int
    char_end: int
    chunk_index: int
    content_sha256: str
    lexical_sha256: str
    token_count: int
    text_original: str
    text_lexical: str
    rank: int
    fusion_score: float
    dense_rank: int | None = None
    dense_score: float | None = None
    sparse_rank: int | None = None
    sparse_score: float | None = None
    exact_rank: int | None = None
    hint_rank: int | None = None
    matched_exact_terms: tuple[str, ...] = ()
    matched_hints: tuple[str, ...] = ()
    is_neighbor: bool = False
    neighbor_of: uuid.UUID | None = None


class HybridSearchBackend(Protocol):
    async def query_branches(
        self,
        *,
        dense: list[float],
        sparse: LexicalSparseVector,
        scope: ResolvedScope,
        dense_limit: int,
        sparse_limit: int,
    ) -> BranchResults: ...

    async def close(self) -> None: ...


class QdrantHybridSearch:
    def __init__(
        self,
        url: str,
        collection: str,
        *,
        client: AsyncQdrantClient | None = None,
    ) -> None:
        self._client = client or AsyncQdrantClient(url=url)
        self._collection = collection
        self._owns_client = client is None

    async def query_branches(
        self,
        *,
        dense: list[float],
        sparse: LexicalSparseVector,
        scope: ResolvedScope,
        dense_limit: int,
        sparse_limit: int,
    ) -> BranchResults:
        if not scope.version_ids:
            return BranchResults(dense=(), sparse=())
        query_filter = _scope_filter(scope)
        dense_request = self._query(
            query=dense,
            using="dense",
            query_filter=query_filter,
            limit=dense_limit,
        )
        if sparse.indices:
            sparse_request = self._query(
                query=models.SparseVector(indices=sparse.indices, values=sparse.values),
                using="sparse",
                query_filter=query_filter,
                limit=sparse_limit,
            )
            dense_hits, sparse_hits = await asyncio.gather(dense_request, sparse_request)
        else:
            dense_hits = await dense_request
            sparse_hits = ()
        return BranchResults(dense=dense_hits, sparse=sparse_hits)

    async def _query(
        self,
        *,
        query: list[float] | models.SparseVector,
        using: str,
        query_filter: models.Filter,
        limit: int,
    ) -> tuple[BranchHit, ...]:
        response = await self._client.query_points(
            collection_name=self._collection,
            query=query,
            using=using,
            query_filter=query_filter,
            limit=limit,
            with_payload=False,
            with_vectors=False,
        )
        hits: list[BranchHit] = []
        for point in response.points:
            try:
                point_id = uuid.UUID(str(point.id))
            except ValueError:
                continue
            hits.append(BranchHit(chunk_id=point_id, score=float(point.score)))
        return tuple(hits)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.close()


class HybridRetriever:
    def __init__(
        self,
        backend: HybridSearchBackend,
        *,
        dense_limit: int,
        sparse_limit: int,
        pool_limit: int,
        section_chunk_limit: int,
        neighbor_radius: int,
        rrf_k: int = RRF_K,
    ) -> None:
        self._backend = backend
        self._dense_limit = dense_limit
        self._sparse_limit = sparse_limit
        self._pool_limit = pool_limit
        self._section_chunk_limit = section_chunk_limit
        self._neighbor_radius = neighbor_radius
        self._rrf_k = rrf_k

    async def retrieve(
        self,
        session: AsyncSession,
        *,
        dense: list[float],
        sparse: LexicalSparseVector,
        exact_terms: tuple[str, ...],
        scope: ResolvedScope,
    ) -> tuple[RetrievalCandidate, ...]:
        branches = await self._backend.query_branches(
            dense=dense,
            sparse=sparse,
            scope=scope,
            dense_limit=self._dense_limit,
            sparse_limit=self._sparse_limit,
        )
        evidence = _fuse_branches(branches, self._rrf_k)
        hydrated = await _hydrate_chunks(
            session,
            tenant_id=scope.tenant_id,
            version_ids=scope.version_ids,
            chunk_ids=tuple(evidence),
        )
        ranked = _rank_hydrated(evidence, hydrated, exact_terms, scope.boosts, self._rrf_k)
        if not ranked:
            return ()

        reserve = min(self._pool_limit // 4, len(ranked)) if self._neighbor_radius else 0
        primary_limit = max(1, self._pool_limit - reserve)
        primaries = _cap_sections(
            ranked,
            limit=primary_limit,
            section_limit=self._section_chunk_limit,
        )
        if self._neighbor_radius == 0 or len(primaries) >= self._pool_limit:
            return tuple(
                candidate.model_copy(update={"rank": rank})
                for rank, candidate in enumerate(primaries, 1)
            )

        neighbors = await _load_neighbors(
            session,
            scope=scope,
            roots=primaries,
            radius=self._neighbor_radius,
            excluded=set(evidence),
        )
        section_counts: dict[uuid.UUID, int] = defaultdict(int)
        for candidate in primaries:
            section_counts[candidate.section_id] += 1
        combined = list(primaries)
        for candidate in neighbors:
            if len(combined) >= self._pool_limit:
                break
            if section_counts[candidate.section_id] >= self._section_chunk_limit:
                continue
            combined.append(candidate)
            section_counts[candidate.section_id] += 1
        return tuple(
            candidate.model_copy(update={"rank": rank})
            for rank, candidate in enumerate(combined, 1)
        )


@dataclass(slots=True)
class _Evidence:
    fusion_score: float = 0.0
    dense_rank: int | None = None
    dense_score: float | None = None
    sparse_rank: int | None = None
    sparse_score: float | None = None
    exact_rank: int | None = None
    hint_rank: int | None = None
    matched_exact_terms: tuple[str, ...] = ()
    matched_hints: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _HydratedChunk:
    chunk: Chunk
    section: Section
    document: Document


def _scope_filter(scope: ResolvedScope) -> models.Filter:
    must: list[models.FieldCondition] = [
        models.FieldCondition(key="tenant_id", match=models.MatchValue(value=str(scope.tenant_id))),
        models.FieldCondition(
            key="document_version_id",
            match=models.MatchAny(any=[str(item) for item in scope.version_ids]),
        ),
        models.FieldCondition(
            key="document_id",
            match=models.MatchAny(any=[str(item) for item in scope.document_ids]),
        ),
    ]
    if scope.section_ids is not None:
        must.append(
            models.FieldCondition(
                key="section_id",
                match=models.MatchAny(any=[str(item) for item in scope.section_ids]),
            )
        )
    return models.Filter(must=must)


def _fuse_branches(branches: BranchResults, rrf_k: int) -> dict[uuid.UUID, _Evidence]:
    result: dict[uuid.UUID, _Evidence] = {}
    for rank, hit in enumerate(branches.dense, start=1):
        evidence = result.setdefault(hit.chunk_id, _Evidence())
        evidence.dense_rank = rank
        evidence.dense_score = hit.score
        evidence.fusion_score += _rrf(rank, rrf_k)
    for rank, hit in enumerate(branches.sparse, start=1):
        evidence = result.setdefault(hit.chunk_id, _Evidence())
        evidence.sparse_rank = rank
        evidence.sparse_score = hit.score
        evidence.fusion_score += _rrf(rank, rrf_k)
    return result


async def _hydrate_chunks(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    version_ids: tuple[uuid.UUID, ...],
    chunk_ids: tuple[uuid.UUID, ...],
) -> dict[uuid.UUID, _HydratedChunk]:
    if not chunk_ids or not version_ids:
        return {}
    rows = (
        await session.execute(
            select(Chunk, Section, Document)
            .join(
                Section,
                (Section.id == Chunk.section_id)
                & (Section.tenant_id == Chunk.tenant_id)
                & (Section.document_version_id == Chunk.document_version_id),
            )
            .join(
                Document,
                (Document.id == Chunk.document_id) & (Document.tenant_id == Chunk.tenant_id),
            )
            .where(
                Chunk.id.in_(chunk_ids),
                Chunk.tenant_id == tenant_id,
                Chunk.document_version_id.in_(version_ids),
                Document.deleted_at.is_(None),
            )
        )
    ).all()
    return {
        chunk.id: _HydratedChunk(chunk=chunk, section=section, document=document)
        for chunk, section, document in rows
    }


def _rank_hydrated(
    evidence: dict[uuid.UUID, _Evidence],
    hydrated: dict[uuid.UUID, _HydratedChunk],
    exact_terms: tuple[str, ...],
    boosts: tuple[HintBoost, ...],
    rrf_k: int,
) -> list[RetrievalCandidate]:
    normalized_terms = tuple(
        (term, normalize_lexical(term)) for term in exact_terms if normalize_lexical(term)
    )
    base_order = sorted(
        hydrated,
        key=lambda item: (-evidence[item].fusion_score, str(item)),
    )
    exact_matches: list[tuple[uuid.UUID, int, int]] = []
    hint_matches: list[tuple[uuid.UUID, int]] = []
    for chunk_id in base_order:
        record = hydrated[chunk_id]
        matched_terms = tuple(
            original
            for original, normalized in normalized_terms
            if normalized in record.chunk.text_lexical
        )
        evidence[chunk_id].matched_exact_terms = matched_terms
        if matched_terms:
            first_position = min(
                record.chunk.text_lexical.find(normalized)
                for _, normalized in normalized_terms
                if normalized in record.chunk.text_lexical
            )
            exact_matches.append((chunk_id, len(matched_terms), first_position))
        matched_hints = tuple(
            boost.hint
            for boost in boosts
            if (boost.target == "document" and record.chunk.document_id in boost.target_ids)
            or (boost.target == "section" and record.chunk.section_id in boost.target_ids)
        )
        evidence[chunk_id].matched_hints = matched_hints
        if matched_hints:
            hint_matches.append((chunk_id, len(matched_hints)))

    exact_matches.sort(
        key=lambda item: (-item[1], item[2], base_order.index(item[0]), str(item[0]))
    )
    for rank, (chunk_id, _, _) in enumerate(exact_matches, start=1):
        evidence[chunk_id].exact_rank = rank
        evidence[chunk_id].fusion_score += _rrf(rank, rrf_k)
    hint_matches.sort(key=lambda item: (-item[1], base_order.index(item[0]), str(item[0])))
    for rank, (chunk_id, _) in enumerate(hint_matches, start=1):
        evidence[chunk_id].hint_rank = rank
        evidence[chunk_id].fusion_score += _rrf(rank, rrf_k)

    final_order = sorted(
        hydrated,
        key=lambda item: (-evidence[item].fusion_score, str(item)),
    )
    return [
        _candidate_from_record(
            hydrated[chunk_id],
            evidence[chunk_id],
            rank=rank,
        )
        for rank, chunk_id in enumerate(final_order, start=1)
    ]


def _candidate_from_record(
    record: _HydratedChunk,
    evidence: _Evidence,
    *,
    rank: int,
    is_neighbor: bool = False,
    neighbor_of: uuid.UUID | None = None,
) -> RetrievalCandidate:
    chunk = record.chunk
    return RetrievalCandidate(
        chunk_id=chunk.id,
        tenant_id=chunk.tenant_id,
        document_id=chunk.document_id,
        document_version_id=chunk.document_version_id,
        section_id=record.section.id,
        document_title=record.document.title,
        source_filename=record.document.source_filename,
        section_path_original=record.section.path_original,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        char_start=chunk.char_start,
        char_end=chunk.char_end,
        chunk_index=chunk.chunk_index,
        content_sha256=chunk.content_sha256,
        lexical_sha256=chunk.lexical_sha256,
        token_count=chunk.token_count,
        text_original=chunk.text_original,
        text_lexical=chunk.text_lexical,
        rank=rank,
        fusion_score=evidence.fusion_score,
        dense_rank=evidence.dense_rank,
        dense_score=evidence.dense_score,
        sparse_rank=evidence.sparse_rank,
        sparse_score=evidence.sparse_score,
        exact_rank=evidence.exact_rank,
        hint_rank=evidence.hint_rank,
        matched_exact_terms=evidence.matched_exact_terms,
        matched_hints=evidence.matched_hints,
        is_neighbor=is_neighbor,
        neighbor_of=neighbor_of,
    )


def _cap_sections(
    candidates: list[RetrievalCandidate], *, limit: int, section_limit: int
) -> list[RetrievalCandidate]:
    selected: list[RetrievalCandidate] = []
    counts: dict[uuid.UUID, int] = defaultdict(int)
    for candidate in candidates:
        if counts[candidate.section_id] >= section_limit:
            continue
        selected.append(candidate)
        counts[candidate.section_id] += 1
        if len(selected) == limit:
            break
    return selected


async def _load_neighbors(
    session: AsyncSession,
    *,
    scope: ResolvedScope,
    roots: list[RetrievalCandidate],
    radius: int,
    excluded: set[uuid.UUID],
) -> list[RetrievalCandidate]:
    conditions = [
        and_(
            Chunk.section_id == root.section_id,
            Chunk.chunk_index >= root.chunk_index - radius,
            Chunk.chunk_index <= root.chunk_index + radius,
        )
        for root in roots
    ]
    if not conditions:
        return []
    rows = (
        await session.execute(
            select(Chunk, Section, Document)
            .join(
                Section,
                (Section.id == Chunk.section_id)
                & (Section.tenant_id == Chunk.tenant_id)
                & (Section.document_version_id == Chunk.document_version_id),
            )
            .join(
                Document,
                (Document.id == Chunk.document_id) & (Document.tenant_id == Chunk.tenant_id),
            )
            .where(
                Chunk.tenant_id == scope.tenant_id,
                Chunk.document_version_id.in_(scope.version_ids),
                Chunk.id.not_in(excluded),
                Document.deleted_at.is_(None),
                or_(*conditions),
            )
        )
    ).all()
    root_rank = {root.chunk_id: rank for rank, root in enumerate(roots)}
    records: list[tuple[int, int, _HydratedChunk, RetrievalCandidate]] = []
    for chunk, section, document in rows:
        possible = [
            root
            for root in roots
            if root.section_id == chunk.section_id
            and abs(root.chunk_index - chunk.chunk_index) <= radius
        ]
        if not possible:
            continue
        root = min(
            possible,
            key=lambda item: (
                abs(item.chunk_index - chunk.chunk_index),
                root_rank[item.chunk_id],
            ),
        )
        evidence = _Evidence(
            fusion_score=root.fusion_score,
            matched_exact_terms=(),
            matched_hints=root.matched_hints,
        )
        record = _HydratedChunk(chunk=chunk, section=section, document=document)
        candidate = _candidate_from_record(
            record,
            evidence,
            rank=0,
            is_neighbor=True,
            neighbor_of=root.chunk_id,
        )
        records.append(
            (
                root_rank[root.chunk_id],
                abs(root.chunk_index - chunk.chunk_index),
                record,
                candidate,
            )
        )
    records.sort(
        key=lambda item: (item[0], item[1], item[2].chunk.chunk_index, str(item[2].chunk.id))
    )
    return [item[3] for item in records]


def _rrf(rank: int, k: int) -> float:
    return 1.0 / (k + rank)
