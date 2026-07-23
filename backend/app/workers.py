from __future__ import annotations

import uuid
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.core.db import create_database_engine, create_session_factory
from app.core.logging import configure_logging
from app.ingest.embedder import TeiClient
from app.ingest.indexer import QdrantChunkIndex
from app.ingest.jobs import IngestionPipeline
from app.ingest.reconcile import reconcile_ingestion_jobs
from app.ingest.storage import LocalDocumentStorage
from app.operations.maintenance import run_maintenance


async def worker_health(_ctx: dict[str, Any]) -> str:
    return "ok"


async def startup(ctx: dict[str, Any]) -> None:
    engine = create_database_engine(settings.resolved_database_url())
    factory = create_session_factory(engine)
    tei = TeiClient(str(settings.embed_url), expected_dimension=settings.embed_dim)
    index = QdrantChunkIndex(
        str(settings.qdrant_url),
        settings.qdrant_collection,
        dense_dimension=settings.embed_dim,
    )
    pipeline = IngestionPipeline(
        factory,
        LocalDocumentStorage(settings.document_storage_root),
        tei,
        tei,
        index,
        chunk_tokens=settings.chunk_tokens,
        chunk_overlap=settings.chunk_overlap,
        batch_size=settings.ingest_batch_size,
        upload_max_bytes=settings.upload_max_mb * 1024 * 1024,
        heartbeat_timeout=settings.ingestion_heartbeat_timeout,
    )
    ctx["engine"] = engine
    ctx["factory"] = factory
    ctx["index"] = index
    ctx["pipeline"] = pipeline


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["index"].close()
    await ctx["engine"].dispose()


async def ingest_document(ctx: dict[str, Any], job_id: str) -> None:
    await ctx["pipeline"].process(uuid.UUID(job_id))


async def reconcile_jobs(ctx: dict[str, Any]) -> list[str]:
    reconciled = await reconcile_ingestion_jobs(
        ctx["factory"],
        ctx["pipeline"],
        stale_after_seconds=settings.ingestion_heartbeat_timeout,
    )
    return [str(job_id) for job_id in reconciled]


async def maintain_data(ctx: dict[str, Any]) -> dict[str, int | bool]:
    if not settings.maintenance_enabled:
        return {"enabled": False}
    result = await run_maintenance(
        ctx["factory"],
        ctx["index"],
        LocalDocumentStorage(settings.document_storage_root),
        retained_generations=settings.retained_index_generations,
        inactive_version_retention_days=settings.inactive_version_retention_days,
        ephemeral_record_retention_days=settings.ephemeral_record_retention_days,
        backup_status_file=settings.backup_status_file,
        gc_backup_max_age_hours=settings.gc_backup_max_age_hours,
    )
    return {
        "enabled": True,
        "garbage_collection_permitted": result.garbage_collection_permitted,
        "refresh_tokens": result.ephemeral.refresh_tokens,
        "idempotency_requests": result.ephemeral.idempotency_requests,
        "versions": result.versions,
        "chunks": result.chunks,
        "files": result.files,
    }


settings = get_settings()
configure_logging(settings.log_level)


class WorkerSettings:
    functions: ClassVar[list[Any]] = [worker_health, ingest_document]
    cron_jobs: ClassVar[list[Any]] = [
        cron(reconcile_jobs, minute=None, second=0, run_at_startup=True),
        cron(maintain_data, hour=3, minute=15, second=0),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings: ClassVar[RedisSettings] = RedisSettings.from_dsn(str(settings.redis_url))
    health_check_interval: ClassVar[int] = 30
    job_timeout: ClassVar[int] = 1800
    max_jobs: ClassVar[int] = 2
