from __future__ import annotations

import hashlib
from typing import Any, Protocol

import orjson
from redis.asyncio import Redis


class JsonCache(Protocol):
    async def get_json(self, key: str) -> dict[str, Any] | None: ...

    async def set_json(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None: ...


class RedisJsonCache:
    def __init__(self, redis_url: str) -> None:
        self._redis: Redis = Redis.from_url(redis_url, decode_responses=False)

    async def get_json(self, key: str) -> dict[str, Any] | None:
        raw = await self._redis.get(key)
        if not isinstance(raw, bytes):
            return None
        value = orjson.loads(raw)
        return value if isinstance(value, dict) else None

    async def set_json(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        await self._redis.set(key, orjson.dumps(value), ex=ttl_seconds)

    async def close(self) -> None:
        await self._redis.aclose()


def cache_key(namespace: str, payload: Any) -> str:
    digest = hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()
    return f"rag:{namespace}:v1:{digest}"
