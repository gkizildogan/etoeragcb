from __future__ import annotations

import asyncio
import math
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.rag.cache import JsonCache, cache_key
from app.rag.candidates import EvidenceCandidate, RerankedEvidence, stable_candidate_key


class Reranker(Protocol):
    async def rerank(
        self, query: str, candidates: tuple[EvidenceCandidate, ...]
    ) -> tuple[RerankedEvidence, ...]: ...


class RerankServiceError(RuntimeError):
    pass


class _CachedScores(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_ids: tuple[str, ...]
    scores: tuple[float, ...]


class TeiReranker:
    """Bounded client for TEI's cross-encoder `/rerank` endpoint."""

    def __init__(
        self,
        base_url: str,
        *,
        model_revision: str,
        max_candidates: int,
        batch_size: int = 32,
        max_retries: int = 2,
        timeout_seconds: float = 30.0,
        cache: JsonCache | None = None,
        cache_ttl: int = 0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if max_candidates < 1 or batch_size < 1:
            raise ValueError("rerank candidate and batch limits must be positive")
        if max_retries < 0:
            raise ValueError("rerank retries cannot be negative")
        self._revision = model_revision
        self._max_candidates = max_candidates
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._cache = cache
        self._cache_ttl = cache_ttl
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds
        )
        self._owns_client = client is None

    async def rerank(
        self, query: str, candidates: tuple[EvidenceCandidate, ...]
    ) -> tuple[RerankedEvidence, ...]:
        bounded = tuple(sorted(candidates, key=stable_candidate_key)[: self._max_candidates])
        if not bounded:
            return ()
        key = cache_key(
            "rerank",
            {
                "query": query,
                "revision": self._revision,
                "candidates": [
                    [item.candidate_id, item.content_sha256, item.lexical_sha256]
                    for item in bounded
                ],
            },
        )
        cached = await self._cache_get(key, bounded)
        scores = cached or await self._score(query, bounded)
        if cached is None:
            await self._cache_set(key, bounded, scores)
        order = sorted(
            range(len(bounded)),
            key=lambda index: (
                -scores[index],
                bounded[index].retrieval_rank,
                bounded[index].candidate_id,
            ),
        )
        return tuple(
            RerankedEvidence(candidate=bounded[index], rerank_score=scores[index], rerank_rank=rank)
            for rank, index in enumerate(order, start=1)
        )

    async def _score(
        self, query: str, candidates: tuple[EvidenceCandidate, ...]
    ) -> tuple[float, ...]:
        batches = [
            candidates[offset : offset + self._batch_size]
            for offset in range(0, len(candidates), self._batch_size)
        ]
        results: list[tuple[float, ...]] = []
        for batch in batches:
            results.append(await self._score_batch(query, batch))
        return tuple(score for batch_scores in results for score in batch_scores)

    async def _score_batch(
        self, query: str, candidates: tuple[EvidenceCandidate, ...]
    ) -> tuple[float, ...]:
        body = {
            "query": query,
            "texts": [candidate.text_original for candidate in candidates],
            "raw_scores": False,
            "return_text": False,
        }
        payload: Any = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post("/rerank", json=body)
                if response.status_code == 429 and attempt < self._max_retries:
                    await asyncio.sleep(_retry_after(response))
                    continue
                response.raise_for_status()
                payload = response.json()
                break
            except (httpx.HTTPError, ValueError) as exc:
                raise RerankServiceError("TEI reranker request failed") from exc
        if payload is None:  # pragma: no cover - loop either parses or raises
            raise RerankServiceError("TEI reranker returned no payload")
        entries = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(entries, list) or len(entries) != len(candidates):
            raise RerankServiceError("TEI reranker returned an invalid result count")
        scores: list[float | None] = [None] * len(candidates)
        for entry in entries:
            if not isinstance(entry, dict):
                raise RerankServiceError("TEI reranker returned a malformed result")
            index = entry.get("index")
            score = entry.get("score")
            if (
                not isinstance(index, int)
                or isinstance(score, bool)
                or not isinstance(score, int | float)
                or index < 0
                or index >= len(candidates)
                or scores[index] is not None
            ):
                raise RerankServiceError("TEI reranker returned an invalid index or score")
            normalized = float(score)
            if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
                raise RerankServiceError("TEI normalized rerank score is outside [0, 1]")
            scores[index] = normalized
        if any(score is None for score in scores):
            raise RerankServiceError("TEI reranker omitted a result")
        return tuple(score for score in scores if score is not None)

    async def _cache_get(
        self, key: str, candidates: tuple[EvidenceCandidate, ...]
    ) -> tuple[float, ...] | None:
        if self._cache is None:
            return None
        try:
            payload = await self._cache.get_json(key)
            if payload is None:
                return None
            cached = _CachedScores.model_validate(payload)
        except (Exception, ValidationError):
            return None
        expected_ids = tuple(candidate.candidate_id for candidate in candidates)
        if cached.candidate_ids != expected_ids or len(cached.scores) != len(candidates):
            return None
        if any(not math.isfinite(score) or not 0.0 <= score <= 1.0 for score in cached.scores):
            return None
        return cached.scores

    async def _cache_set(
        self,
        key: str,
        candidates: tuple[EvidenceCandidate, ...],
        scores: tuple[float, ...],
    ) -> None:
        if self._cache is None or self._cache_ttl <= 0:
            return
        payload = _CachedScores(
            candidate_ids=tuple(candidate.candidate_id for candidate in candidates),
            scores=scores,
        )
        try:
            await self._cache.set_json(key, payload.model_dump(mode="json"), self._cache_ttl)
        except Exception:
            return

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _retry_after(response: httpx.Response) -> float:
    raw = response.headers.get("retry-after")
    try:
        delay = float(raw) if raw is not None else 0.25
    except ValueError:
        return 0.25
    return max(0.05, min(delay, 2.0))
