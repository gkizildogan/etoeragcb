from __future__ import annotations

import asyncio
import uuid
from typing import Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import Metrics
from app.rag.candidates import EvidenceCandidate, document_evidence
from app.rag.postprocess import PostRetrievalResult, PostRetrievalService
from app.rag.service import RetrievalResult
from app.rag.web import WebFailure, WebRetrievalResult


class DocumentRetriever(Protocol):
    async def retrieve(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        message: str,
        explicit_document_ids: tuple[uuid.UUID, ...] = (),
        explicit_collection_ids: tuple[uuid.UUID, ...] = (),
    ) -> RetrievalResult: ...


class WebBranch(Protocol):
    async def retrieve(self, query: str) -> WebRetrievalResult: ...


class CombinedRetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    documents: RetrievalResult
    web: WebRetrievalResult
    combined_pool: tuple[EvidenceCandidate, ...]
    post_retrieval: PostRetrievalResult


class CombinedRetrievalService:
    def __init__(
        self,
        documents: DocumentRetriever,
        web: WebBranch,
        post_retrieval: PostRetrievalService,
        *,
        pool_limit: int,
        metrics: Metrics | None = None,
    ) -> None:
        if pool_limit < 1:
            raise ValueError("combined pool limit must be positive")
        self._documents = documents
        self._web = web
        self._post_retrieval = post_retrieval
        self._pool_limit = pool_limit
        self._metrics = metrics

    async def retrieve(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        message: str,
        web_search: bool,
        explicit_document_ids: tuple[uuid.UUID, ...] = (),
        explicit_collection_ids: tuple[uuid.UUID, ...] = (),
    ) -> CombinedRetrievalResult:
        document_task = asyncio.create_task(
            self._documents.retrieve(
                session,
                tenant_id=tenant_id,
                message=message,
                explicit_document_ids=explicit_document_ids,
                explicit_collection_ids=explicit_collection_ids,
            )
        )
        web_task = asyncio.create_task(self._safe_web(message)) if web_search else None
        try:
            documents = await document_task
        except BaseException:
            if web_task is not None:
                web_task.cancel()
                await asyncio.gather(web_task, return_exceptions=True)
            raise
        web = await web_task if web_task is not None else WebRetrievalResult(status="disabled")
        if self._metrics is not None:
            self._record_metrics(web)
        document_candidates = document_evidence(documents.candidates)
        combined = merge_candidate_pool(
            document_candidates,
            web.candidates,
            limit=self._pool_limit,
        )
        post = await self._post_retrieval.process(
            query=documents.planning.plan.query,
            candidates=combined,
        )
        return CombinedRetrievalResult(
            documents=documents,
            web=web,
            combined_pool=combined,
            post_retrieval=post,
        )

    async def _safe_web(self, query: str) -> WebRetrievalResult:
        try:
            return await self._web.retrieve(query)
        except Exception:
            return WebRetrievalResult(
                status="failed",
                failures=(WebFailure(code="web_branch_unexpected"),),
            )

    def _record_metrics(self, result: WebRetrievalResult) -> None:
        assert self._metrics is not None
        self._metrics.web_retrieval.labels(result.status).inc()
        if result.candidates:
            self._metrics.web_fetch.labels("accepted", "none").inc(len(result.candidates))
        for failure in result.failures:
            self._metrics.web_fetch.labels("rejected", failure.code).inc()


def merge_candidate_pool(
    documents: tuple[EvidenceCandidate, ...],
    web: tuple[EvidenceCandidate, ...],
    *,
    limit: int,
) -> tuple[EvidenceCandidate, ...]:
    ordered_documents = sorted(documents, key=lambda item: (item.retrieval_rank, item.candidate_id))
    ordered_web = sorted(web, key=lambda item: (item.retrieval_rank, item.candidate_id))
    merged: list[EvidenceCandidate] = []
    index = 0
    while len(merged) < limit and (index < len(ordered_documents) or index < len(ordered_web)):
        if index < len(ordered_documents):
            merged.append(ordered_documents[index])
            if len(merged) >= limit:
                break
        if index < len(ordered_web):
            merged.append(ordered_web[index])
        index += 1
    return tuple(
        candidate.model_copy(
            update={
                "retrieval_rank": rank,
                "provenance": {
                    **candidate.provenance,
                    "branch_retrieval_rank": candidate.retrieval_rank,
                    "combined_pool_rank": rank,
                },
            }
        )
        for rank, candidate in enumerate(merged[:limit], start=1)
    )
