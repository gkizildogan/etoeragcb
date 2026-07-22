from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingest.indexer import ChunkIndex
from app.ingest.jobs import IngestionPipeline, _copy_manifest
from app.ingest.storage import LocalDocumentStorage
from app.models import (
    Chunk,
    Document,
    DocumentVersion,
    IndexGeneration,
    IndexGenerationDocument,
    IngestionJob,
    Section,
    Tenant,
)

logger = structlog.get_logger(__name__)


async def reconcile_ingestion_jobs(
    session_factory: async_sessionmaker[AsyncSession],
    pipeline: IngestionPipeline,
    *,
    stale_after_seconds: int,
    now: datetime | None = None,
) -> list[uuid.UUID]:
    checked_at = now or datetime.now(UTC)
    stale_before = checked_at - timedelta(seconds=stale_after_seconds)
    async with session_factory() as session:
        job_ids = list(
            await session.scalars(
                select(IngestionJob.id)
                .where(
                    or_(
                        IngestionJob.status.in_(("staged", "queued")),
                        (
                            (IngestionJob.status == "processing")
                            & or_(
                                IngestionJob.heartbeat_at.is_(None),
                                IngestionJob.heartbeat_at < stale_before,
                            )
                        ),
                    )
                )
                .order_by(IngestionJob.created_at, IngestionJob.id)
            )
        )
    reconciled: list[uuid.UUID] = []
    for job_id in job_ids:
        try:
            await pipeline.process(job_id)
        except Exception as exc:
            logger.warning(
                "ingestion_reconciliation_failed",
                job_id=str(job_id),
                error_type=type(exc).__name__,
            )
            continue
        reconciled.append(job_id)
    return reconciled


async def tombstone_document(
    session: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> int:
    now = datetime.now(UTC)
    tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    document = await session.scalar(
        select(Document)
        .where(
            Document.id == document_id,
            Document.tenant_id == tenant_id,
            Document.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if tenant is None or document is None:
        raise LookupError("document does not exist")
    pending_version_ids = list(
        await session.scalars(
            select(DocumentVersion.id).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.tenant_id == tenant_id,
                DocumentVersion.status.in_(("staged", "processing", "ready")),
            )
        )
    )
    if pending_version_ids:
        await session.execute(
            update(DocumentVersion)
            .where(DocumentVersion.id.in_(pending_version_ids))
            .values(
                status="failed",
                error_code="DOCUMENT_DELETED",
                error_detail="Document was deleted before activation",
            )
        )
        await session.execute(
            update(IngestionJob)
            .where(IngestionJob.document_version_id.in_(pending_version_ids))
            .values(status="failed", error="Document was deleted before activation")
        )
        await session.execute(
            update(IndexGeneration)
            .where(
                IndexGeneration.changed_document_version_id.in_(pending_version_ids),
                IndexGeneration.status == "preparing",
            )
            .values(status="failed")
        )
    generation = IndexGeneration(
        tenant_id=tenant_id,
        reason="document_delete",
        parent_generation_id=tenant.active_index_generation_id,
        status="preparing",
        retrieval_revision=tenant.retrieval_revision + 1,
    )
    session.add(generation)
    await session.flush()
    await _copy_manifest(
        session,
        source_generation_id=tenant.active_index_generation_id,
        target_generation_id=generation.id,
        tenant_id=tenant_id,
        exclude_document_id=document.id,
    )
    if document.active_version_id is not None:
        active_version = await session.scalar(
            select(DocumentVersion).where(DocumentVersion.id == document.active_version_id)
        )
        if active_version is not None:
            active_version.status = "superseded"
    revision = tenant.retrieval_revision + 1
    document.deleted_at = now
    document.active_version_id = None
    generation.status = "active"
    generation.activated_at = now
    tenant.active_index_generation_id = generation.id
    tenant.retrieval_revision = revision
    await session.commit()
    return revision


async def active_manifest_version_ids(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[uuid.UUID]:
    generation_id = await session.scalar(
        select(Tenant.active_index_generation_id).where(Tenant.id == tenant_id)
    )
    if generation_id is None:
        return []
    return list(
        await session.scalars(
            select(IndexGenerationDocument.document_version_id)
            .join(
                Document,
                (Document.id == IndexGenerationDocument.document_id)
                & (Document.tenant_id == IndexGenerationDocument.tenant_id),
            )
            .where(
                IndexGenerationDocument.generation_id == generation_id,
                IndexGenerationDocument.tenant_id == tenant_id,
                Document.deleted_at.is_(None),
            )
            .order_by(IndexGenerationDocument.document_id)
        )
    )


@dataclass(frozen=True, slots=True)
class GarbageCollectionResult:
    versions: int
    chunks: int
    files: int


async def garbage_collect_inactive_versions(
    session_factory: async_sessionmaker[AsyncSession],
    index: ChunkIndex,
    storage: LocalDocumentStorage,
    *,
    tenant_id: uuid.UUID,
    retained_generations: int,
) -> GarbageCollectionResult:
    async with session_factory() as session:
        generation_ids = list(
            await session.scalars(
                select(IndexGeneration.id)
                .where(
                    IndexGeneration.tenant_id == tenant_id,
                    IndexGeneration.status == "active",
                )
                .order_by(IndexGeneration.activated_at.desc(), IndexGeneration.id.desc())
                .limit(retained_generations)
            )
        )
        protected_versions = set(
            await session.scalars(
                select(IndexGenerationDocument.document_version_id).where(
                    IndexGenerationDocument.tenant_id == tenant_id,
                    IndexGenerationDocument.generation_id.in_(generation_ids),
                )
            )
        )
        protected_versions.update(
            await session.scalars(
                select(DocumentVersion.id).where(
                    DocumentVersion.tenant_id == tenant_id,
                    DocumentVersion.status.in_(("staged", "processing", "ready", "active")),
                )
            )
        )
        statement = select(DocumentVersion).where(
            DocumentVersion.tenant_id == tenant_id,
            DocumentVersion.status.in_(("failed", "superseded")),
        )
        if protected_versions:
            statement = statement.where(DocumentVersion.id.not_in(protected_versions))
        candidates = list(await session.scalars(statement))
        version_ids = [version.id for version in candidates]
        chunk_count = 0
        if version_ids:
            chunk_count = len(
                list(
                    await session.scalars(
                        select(Chunk.id).where(Chunk.document_version_id.in_(version_ids))
                    )
                )
            )
    if not candidates:
        return GarbageCollectionResult(versions=0, chunks=0, files=0)
    await index.delete_versions(tenant_id=tenant_id, version_ids=version_ids)
    deleted_files = 0
    for version in candidates:
        if storage.resolve(version.storage_key).exists():
            storage.delete(version.storage_key)
            deleted_files += 1
    async with session_factory() as session:
        await session.execute(delete(Chunk).where(Chunk.document_version_id.in_(version_ids)))
        await session.execute(delete(Section).where(Section.document_version_id.in_(version_ids)))
        await session.commit()
    return GarbageCollectionResult(
        versions=len(version_ids), chunks=chunk_count, files=deleted_files
    )
