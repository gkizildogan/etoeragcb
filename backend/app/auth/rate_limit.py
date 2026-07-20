from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from redis.asyncio import Redis


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0


class RateLimiter(Protocol):
    async def check(
        self, scope: str, identifiers: dict[str, str], limits: list[str]
    ) -> RateLimitDecision: ...

    async def register_failure(self, subject: str) -> None: ...

    async def clear_failures(self, subject: str) -> None: ...

    async def close(self) -> None: ...


class RedisRateLimiter:
    _increment_script = """
    local current = redis.call('INCR', KEYS[1])
    if current == 1 then
        redis.call('EXPIRE', KEYS[1], ARGV[1])
    end
    return {current, redis.call('TTL', KEYS[1])}
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def check(
        self, scope: str, identifiers: dict[str, str], limits: list[str]
    ) -> RateLimitDecision:
        retry_after = 0
        for label, identifier in identifiers.items():
            for raw_limit in limits:
                maximum, window = (int(part) for part in raw_limit.split("/", maxsplit=1))
                bucket = f"rate:{scope}:{label}:{identifier}:{window}"
                pending = self._redis.eval(self._increment_script, 1, bucket, str(window))
                current, ttl = await cast(Awaitable[Any], pending)
                if int(current) > maximum:
                    retry_after = max(retry_after, max(1, int(ttl)))
        return RateLimitDecision(allowed=retry_after == 0, retry_after=retry_after)

    async def register_failure(self, subject: str) -> None:
        key = f"rate:login:failures:{subject}"
        failures = int(await self._redis.incr(key))
        if failures == 1:
            await self._redis.expire(key, 3600)
        delay = min(2.0, 0.125 * math.pow(2, min(failures - 1, 4)))
        await asyncio.sleep(delay)

    async def clear_failures(self, subject: str) -> None:
        await self._redis.delete(f"rate:login:failures:{subject}")

    async def close(self) -> None:
        await self._redis.aclose()
