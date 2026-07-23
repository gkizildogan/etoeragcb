from __future__ import annotations

import hashlib
import time
from typing import Any, Protocol

import orjson
from redis.asyncio import Redis


class JsonCache(Protocol):
    async def get_json(self, key: str) -> dict[str, Any] | None: ...

    async def set_json(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None: ...


class CacheMetrics(Protocol):
    cache_operations: Any
    cache_operation_duration: Any


class RedisJsonCache:
    def __init__(self, redis_url: str, metrics: CacheMetrics | None = None) -> None:
        self._redis: Redis = Redis.from_url(redis_url, decode_responses=False)
        self._metrics = metrics

    async def get_json(self, key: str) -> dict[str, Any] | None:
        namespace = _cache_namespace(key)
        started = time.perf_counter()
        outcome = "error"
        try:
            raw = await self._redis.get(key)
            if not isinstance(raw, bytes):
                outcome = "miss"
                return None
            value = orjson.loads(raw)
            if not isinstance(value, dict):
                return None
            outcome = "hit"
            return value
        finally:
            self._observe(namespace, "get", outcome, time.perf_counter() - started)

    async def set_json(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        namespace = _cache_namespace(key)
        if ttl_seconds <= 0:
            self._observe(namespace, "set", "skipped", 0.0)
            return
        started = time.perf_counter()
        outcome = "error"
        try:
            await self._redis.set(key, orjson.dumps(value), ex=ttl_seconds)
            outcome = "stored"
        finally:
            self._observe(namespace, "set", outcome, time.perf_counter() - started)

    async def close(self) -> None:
        await self._redis.aclose()

    def _observe(
        self, namespace: str, operation: str, outcome: str, duration_seconds: float
    ) -> None:
        if self._metrics is None:
            return
        self._metrics.cache_operations.labels(namespace, operation, outcome).inc()
        self._metrics.cache_operation_duration.labels(namespace, operation).observe(
            duration_seconds
        )


def cache_key(namespace: str, payload: Any) -> str:
    digest = hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()
    return f"rag:{namespace}:v1:{digest}"


def _cache_namespace(key: str) -> str:
    parts = key.split(":", maxsplit=3)
    if (
        len(parts) >= 3
        and parts[0] == "rag"
        and parts[1]
        in {
            "answer",
            "embedding",
            "plan",
            "rerank",
            "retrieval",
        }
    ):
        return parts[1]
    return "other"
