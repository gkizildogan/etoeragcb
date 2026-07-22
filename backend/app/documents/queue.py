from __future__ import annotations

import uuid
from typing import Protocol

from arq import create_pool
from arq.connections import RedisSettings


class IngestionQueue(Protocol):
    async def enqueue(self, job_id: uuid.UUID) -> str | None: ...


class ArqIngestionQueue:
    def __init__(self, redis_url: str) -> None:
        self._settings = RedisSettings.from_dsn(redis_url)

    async def enqueue(self, job_id: uuid.UUID) -> str | None:
        pool = await create_pool(self._settings)
        try:
            job = await pool.enqueue_job(
                "ingest_document", str(job_id), _job_id=f"ingestion:{job_id}"
            )
            return job.job_id if job is not None else None
        finally:
            await pool.aclose()
