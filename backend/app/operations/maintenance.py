from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingest.indexer import ChunkIndex
from app.ingest.reconcile import GarbageCollectionResult, garbage_collect_inactive_versions
from app.ingest.storage import LocalDocumentStorage
from app.models import IdempotencyRequest, RefreshToken, Tenant
from app.operations.status import has_recent_off_machine_backup

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class EphemeralRetentionResult:
    refresh_tokens: int
    idempotency_requests: int


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    ephemeral: EphemeralRetentionResult
    garbage_collection_permitted: bool
    tenants_checked: int
    versions: int
    chunks: int
    files: int


async def prune_ephemeral_records(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    older_than: datetime,
) -> EphemeralRetentionResult:
    async with session_factory() as session:
        refresh_filter = or_(
            RefreshToken.expires_at < older_than,
            RefreshToken.revoked_at < older_than,
        )
        refresh_count = int(
            await session.scalar(
                select(func.count()).select_from(RefreshToken).where(refresh_filter)
            )
            or 0
        )
        idempotency_filter = IdempotencyRequest.expires_at < older_than
        idempotency_count = int(
            await session.scalar(
                select(func.count()).select_from(IdempotencyRequest).where(idempotency_filter)
            )
            or 0
        )
        await session.execute(delete(RefreshToken).where(refresh_filter))
        await session.execute(delete(IdempotencyRequest).where(idempotency_filter))
        await session.commit()
    return EphemeralRetentionResult(
        refresh_tokens=refresh_count,
        idempotency_requests=idempotency_count,
    )


async def run_maintenance(
    session_factory: async_sessionmaker[AsyncSession],
    index: ChunkIndex,
    storage: LocalDocumentStorage,
    *,
    retained_generations: int,
    inactive_version_retention_days: int,
    ephemeral_record_retention_days: int,
    backup_status_file: Path,
    gc_backup_max_age_hours: int,
    now: datetime | None = None,
) -> MaintenanceResult:
    checked_at = now or datetime.now(UTC)
    ephemeral = await prune_ephemeral_records(
        session_factory,
        older_than=checked_at - timedelta(days=ephemeral_record_retention_days),
    )
    backup_recent = has_recent_off_machine_backup(
        backup_status_file,
        max_age_hours=gc_backup_max_age_hours,
        now=checked_at,
    )
    if not backup_recent:
        logger.warning(
            "payload_gc_skipped",
            reason="no_recent_verified_off_machine_backup",
            refresh_tokens_pruned=ephemeral.refresh_tokens,
            idempotency_requests_pruned=ephemeral.idempotency_requests,
        )
        return MaintenanceResult(
            ephemeral=ephemeral,
            garbage_collection_permitted=False,
            tenants_checked=0,
            versions=0,
            chunks=0,
            files=0,
        )

    async with session_factory() as session:
        tenant_ids = list(await session.scalars(select(Tenant.id).order_by(Tenant.id)))
    totals = GarbageCollectionResult(versions=0, chunks=0, files=0)
    cutoff = checked_at - timedelta(days=inactive_version_retention_days)
    for tenant_id in tenant_ids:
        result = await garbage_collect_inactive_versions(
            session_factory,
            index,
            storage,
            tenant_id=uuid.UUID(str(tenant_id)),
            retained_generations=retained_generations,
            older_than=cutoff,
            now=checked_at,
        )
        totals = GarbageCollectionResult(
            versions=totals.versions + result.versions,
            chunks=totals.chunks + result.chunks,
            files=totals.files + result.files,
        )
    logger.info(
        "maintenance_completed",
        tenants_checked=len(tenant_ids),
        refresh_tokens_pruned=ephemeral.refresh_tokens,
        idempotency_requests_pruned=ephemeral.idempotency_requests,
        versions_collected=totals.versions,
        chunks_collected=totals.chunks,
        files_collected=totals.files,
    )
    return MaintenanceResult(
        ephemeral=ephemeral,
        garbage_collection_permitted=True,
        tenants_checked=len(tenant_ids),
        versions=totals.versions,
        chunks=totals.chunks,
        files=totals.files,
    )
