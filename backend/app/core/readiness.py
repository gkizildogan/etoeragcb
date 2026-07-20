from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.core.db import create_database_engine

Probe = Callable[[], Awaitable[None]]


class ReadinessChecker:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine = create_database_engine(settings.resolved_database_url())
        self._redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
        self._http = httpx.AsyncClient(timeout=settings.readiness_timeout_seconds)

    async def _database(self) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def _redis_ping(self) -> None:
        if not await self._redis.ping():
            raise RuntimeError("redis ping returned false")

    async def _get(self, base_url: object, path: str) -> None:
        response = await self._http.get(f"{str(base_url).rstrip('/')}{path}")
        response.raise_for_status()

    async def check(self) -> dict[str, bool]:
        probes: dict[str, Probe] = {
            "postgres": self._database,
            "redis": self._redis_ping,
            "qdrant": lambda: self._get(self._settings.qdrant_url, "/readyz"),
            "vllm": lambda: self._get(self._settings.vllm_base_url, "/health"),
            "embed": lambda: self._get(self._settings.embed_url, "/health"),
            "rerank": lambda: self._get(self._settings.rerank_url, "/health"),
        }

        async def run(probe: Probe) -> bool:
            try:
                await probe()
            except Exception:
                return False
            return True

        values = await asyncio.gather(*(run(probe) for probe in probes.values()))
        return dict(zip(probes, values, strict=True))

    async def close(self) -> None:
        await self._http.aclose()
        await self._redis.aclose()
        await self._engine.dispose()
