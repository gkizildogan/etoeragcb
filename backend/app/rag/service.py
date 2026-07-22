from __future__ import annotations

import uuid
from typing import Protocol

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.hashing import sparse_lexical_vector
from app.ingest.normalization import normalize_lexical
from app.rag.cache import JsonCache, cache_key
from app.rag.planner import Planner, PlanningResult
from app.rag.retriever import HybridRetriever, RetrievalCandidate
from app.rag.scope import MetadataResolver, ResolvedScope


class QueryEmbedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class RetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    planning: PlanningResult
    scope: ResolvedScope
    candidates: tuple[RetrievalCandidate, ...]
    cache_hit: bool = False


class RetrievalService:
    def __init__(
        self,
        planner: Planner,
        resolver: MetadataResolver,
        embedder: QueryEmbedder,
        retriever: HybridRetriever,
        *,
        cache: JsonCache | None = None,
        plan_cache_ttl: int = 0,
        retrieval_cache_ttl: int = 0,
        planner_revision: str,
        embedding_revision: str,
        retrieval_signature: str = "rrf60-v1",
    ) -> None:
        self._planner = planner
        self._resolver = resolver
        self._embedder = embedder
        self._retriever = retriever
        self._cache = cache
        self._plan_cache_ttl = plan_cache_ttl
        self._retrieval_cache_ttl = retrieval_cache_ttl
        self._planner_revision = planner_revision
        self._embedding_revision = embedding_revision
        self._retrieval_signature = retrieval_signature

    async def retrieve(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        message: str,
        explicit_document_ids: tuple[uuid.UUID, ...] = (),
        explicit_collection_ids: tuple[uuid.UUID, ...] = (),
    ) -> RetrievalResult:
        normalized_document_ids = tuple(sorted(set(explicit_document_ids), key=str))
        normalized_collection_ids = tuple(sorted(set(explicit_collection_ids), key=str))
        planning = await self._planning(tenant_id, message)
        scope = await self._resolver.resolve(
            session,
            tenant_id=tenant_id,
            plan=planning.plan,
            explicit_document_ids=normalized_document_ids,
            explicit_collection_ids=normalized_collection_ids,
        )
        retrieval_key = cache_key(
            "retrieval",
            {
                "tenant_id": str(tenant_id),
                "generation_id": scope.generation_id,
                "retrieval_revision": scope.retrieval_revision,
                "plan": planning.plan.model_dump(mode="json"),
                "document_ids": [str(item) for item in normalized_document_ids],
                "collection_ids": [str(item) for item in normalized_collection_ids],
                "embedding_revision": self._embedding_revision,
                "retrieval_signature": self._retrieval_signature,
            },
        )
        cached = await self._cache_get(retrieval_key)
        if cached is not None:
            try:
                result = RetrievalResult.model_validate(cached)
                return result.model_copy(update={"cache_hit": True})
            except ValidationError:
                pass

        candidates: tuple[RetrievalCandidate, ...] = ()
        if planning.plan.intent == "knowledge" and scope.version_ids:
            embeddings = await self._embedder.embed([planning.plan.query])
            if len(embeddings) != 1:
                raise RuntimeError("query embedder returned an invalid embedding count")
            candidates = await self._retriever.retrieve(
                session,
                dense=embeddings[0],
                sparse=sparse_lexical_vector(normalize_lexical(planning.plan.query)),
                exact_terms=tuple(planning.plan.exact_terms),
                scope=scope,
            )
        result = RetrievalResult(
            planning=planning,
            scope=scope,
            candidates=candidates,
        )
        await self._cache_set(
            retrieval_key,
            result.model_dump(mode="json"),
            self._retrieval_cache_ttl,
        )
        return result

    async def _planning(self, tenant_id: uuid.UUID, message: str) -> PlanningResult:
        key = cache_key(
            "plan",
            {
                "tenant_id": str(tenant_id),
                "message": message,
                "planner_revision": self._planner_revision,
            },
        )
        cached = await self._cache_get(key)
        if cached is not None:
            try:
                return PlanningResult.model_validate(cached)
            except ValidationError:
                pass
        result = await self._planner.plan(message)
        await self._cache_set(key, result.model_dump(mode="json"), self._plan_cache_ttl)
        return result

    async def _cache_get(self, key: str) -> dict[str, object] | None:
        if self._cache is None:
            return None
        try:
            return await self._cache.get_json(key)
        except Exception:
            return None

    async def _cache_set(self, key: str, value: dict[str, object], ttl_seconds: int) -> None:
        if self._cache is None or ttl_seconds <= 0:
            return
        try:
            await self._cache.set_json(key, value, ttl_seconds)
        except Exception:
            return
