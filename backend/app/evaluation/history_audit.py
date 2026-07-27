from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal, Protocol

from qdrant_client import AsyncQdrantClient, models
from sqlalchemy import false, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.evaluation.history import HistoricalTurn
from app.ingest.chunker import Tokenizer, chunk_blocks
from app.ingest.parsers import parse_document
from app.ingest.sections import build_sections
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

AuditStatus = Literal["PASS", "FAIL", "WARN"]


class PointReader(Protocol):
    async def points_for_version(
        self, tenant_id: uuid.UUID, version_id: uuid.UUID
    ) -> tuple[dict[str, Any], ...]: ...

    async def close(self) -> None: ...


class QdrantPointReader:
    def __init__(
        self,
        url: str,
        collection: str,
        *,
        client: AsyncQdrantClient | None = None,
    ) -> None:
        self._client = client or AsyncQdrantClient(url=url, check_compatibility=False)
        self._collection = collection
        self._owns_client = client is None

    async def points_for_version(
        self, tenant_id: uuid.UUID, version_id: uuid.UUID
    ) -> tuple[dict[str, Any], ...]:
        query_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="tenant_id", match=models.MatchValue(value=str(tenant_id))
                ),
                models.FieldCondition(
                    key="document_version_id",
                    match=models.MatchValue(value=str(version_id)),
                ),
            ]
        )
        offset: models.ExtendedPointId | None = None
        records: list[dict[str, Any]] = []
        while True:
            points, next_offset = await self._client.scroll(
                collection_name=self._collection,
                scroll_filter=query_filter,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            for point in points:
                vector = point.vector
                vector_names = set(vector) if isinstance(vector, dict) else {"dense"}
                records.append(
                    {
                        "id": str(point.id),
                        "payload": dict(point.payload or {}),
                        "vector_names": sorted(vector_names),
                    }
                )
            if next_offset is None:
                break
            offset = next_offset
        return tuple(records)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.close()


async def audit_ingestion(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    turns: tuple[HistoricalTurn, ...],
    settings: Settings,
    tokenizer: Tokenizer,
    point_reader: PointReader,
) -> dict[str, Any]:
    tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
    active_generation_id = tenant.active_index_generation_id if tenant is not None else None
    active_version_ids = set(
        await session.scalars(
            select(IndexGenerationDocument.document_version_id)
            .join(
                Tenant,
                (Tenant.id == IndexGenerationDocument.tenant_id)
                & (Tenant.active_index_generation_id == IndexGenerationDocument.generation_id),
            )
            .where(IndexGenerationDocument.tenant_id == tenant_id)
        )
    )
    active_version_ids.update(
        item
        for item in await session.scalars(
            select(Document.active_version_id).where(
                Document.tenant_id == tenant_id,
                Document.deleted_at.is_(None),
                Document.active_version_id.is_not(None),
            )
        )
        if item is not None
    )
    referenced_version_ids = _referenced_versions(turns)
    recorded_provenance = _recorded_provenance_by_version(turns)
    all_ids = active_version_ids | referenced_version_ids
    versions = {
        version.id: version
        for version in await session.scalars(
            select(DocumentVersion).where(
                DocumentVersion.tenant_id == tenant_id,
                DocumentVersion.id.in_(all_ids) if all_ids else false(),
            )
        )
    }
    records: list[dict[str, Any]] = []
    for version_id in sorted(all_ids, key=str):
        version = versions.get(version_id)
        is_active = version_id in active_version_ids
        if version is None:
            records.append(
                _version_result(
                    version_id,
                    is_active,
                    "FAIL" if is_active else "WARN",
                    [
                        _check(
                            "postgres_version",
                            "FAIL" if is_active else "WARN",
                            "version row is unavailable",
                        )
                    ],
                    unavailable_by_retention=not is_active,
                )
            )
            continue
        records.append(
            await _audit_version(
                session,
                version=version,
                is_active=is_active,
                settings=settings,
                tokenizer=tokenizer,
                point_reader=point_reader,
                recorded_provenance=recorded_provenance.get(version_id, ()),
                expected_generation_id=(
                    active_generation_id if is_active else version.index_generation_id
                ),
                historical_referenced=version_id in referenced_version_ids,
            )
        )
    failed_active = [
        record["version_id"]
        for record in records
        if record["active"] and record["status"] == "FAIL"
    ]
    return {
        "schema_version": 1,
        "tenant_id": str(tenant_id),
        "summary": {
            "versions": len(records),
            "active_versions": len(active_version_ids),
            "historical_referenced_versions": len(referenced_version_ids),
            "pass": sum(item["status"] == "PASS" for item in records),
            "warn": sum(item["status"] == "WARN" for item in records),
            "fail": sum(item["status"] == "FAIL" for item in records),
            "failed_active_versions": failed_active,
        },
        "versions": records,
    }


async def _audit_version(
    session: AsyncSession,
    *,
    version: DocumentVersion,
    is_active: bool,
    settings: Settings,
    tokenizer: Tokenizer,
    point_reader: PointReader,
    recorded_provenance: tuple[dict[str, str], ...],
    expected_generation_id: int | None,
    historical_referenced: bool,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    retention = version.garbage_collected_at is not None
    storage = LocalDocumentStorage(settings.document_storage_root)
    source_path = storage.resolve(version.storage_key)
    if retention and not source_path.exists():
        checks.append(
            _check(
                "retention",
                "WARN",
                "raw file and derived evidence are unavailable by retention",
            )
        )
        return _version_result(
            version.id,
            is_active,
            "FAIL" if is_active else "WARN",
            checks,
            unavailable_by_retention=True,
        )

    raw_hash = _sha256_path(source_path) if source_path.exists() else None
    checks.append(
        _check(
            "raw_file_sha256",
            "PASS" if raw_hash == version.file_sha256 else "FAIL",
            {"expected": version.file_sha256, "actual": raw_hash},
        )
    )
    revision_mismatches = [
        provenance
        for provenance in recorded_provenance
        if (
            provenance.get("embedding_revision") not in {None, settings.embed_revision}
            or provenance.get("reranker_revision") not in {None, settings.rerank_revision}
        )
    ]
    if revision_mismatches:
        checks.append(
            _check(
                "historical_model_revision_comparability",
                "WARN",
                {
                    "comparable": False,
                    "current_embedding_revision": settings.embed_revision,
                    "current_reranker_revision": settings.rerank_revision,
                    "recorded": revision_mismatches,
                },
            )
        )
    elif historical_referenced and not recorded_provenance:
        checks.append(
            _check(
                "historical_model_revision_comparability",
                "WARN",
                {
                    "comparable": None,
                    "reason": "legacy_message_has_no_model_revision_provenance",
                },
            )
        )
    job = await session.scalar(
        select(IngestionJob).where(
            IngestionJob.tenant_id == version.tenant_id,
            IngestionJob.document_version_id == version.id,
        )
    )
    lifecycle_ok = (
        job is not None
        and job.status == "succeeded"
        and version.status in {"ready", "active", "superseded"}
        and version.index_generation_id is not None
    )
    checks.append(
        _check(
            "version_job_generation_lifecycle",
            "PASS" if lifecycle_ok else "FAIL",
            {
                "version_status": version.status,
                "job_status": job.status if job is not None else None,
                "generation_id": version.index_generation_id,
            },
        )
    )
    manifest = await session.scalar(
        select(IndexGenerationDocument).where(
            IndexGenerationDocument.tenant_id == version.tenant_id,
            IndexGenerationDocument.document_version_id == version.id,
            IndexGenerationDocument.generation_id == expected_generation_id,
        )
    )
    generation = (
        await session.scalar(
            select(IndexGeneration).where(
                IndexGeneration.id == expected_generation_id,
                IndexGeneration.tenant_id == version.tenant_id,
            )
        )
        if expected_generation_id is not None
        else None
    )
    checks.append(
        _check(
            "generation_manifest",
            (
                "PASS"
                if manifest is not None and generation is not None and generation.status == "active"
                else "FAIL"
            ),
            {
                "manifest_member": manifest is not None,
                "expected_generation_id": expected_generation_id,
                "generation_status": generation.status if generation is not None else None,
            },
        )
    )
    sections = list(
        await session.scalars(
            select(Section)
            .where(
                Section.tenant_id == version.tenant_id,
                Section.document_version_id == version.id,
            )
            .order_by(Section.ordinal)
        )
    )
    chunks = list(
        await session.scalars(
            select(Chunk)
            .where(
                Chunk.tenant_id == version.tenant_id,
                Chunk.document_version_id == version.id,
            )
            .order_by(Chunk.chunk_index)
        )
    )
    actual_pages = len({chunk.page_start for chunk in chunks})
    counts_ok = len(sections) == version.section_count and len(chunks) == version.chunk_count
    checks.append(
        _check(
            "postgres_counts",
            "PASS" if counts_ok else "FAIL",
            {
                "expected": {
                    "pages": version.page_count,
                    "sections": version.section_count,
                    "chunks": version.chunk_count,
                },
                "actual": {
                    "pages_represented_by_chunks": actual_pages,
                    "sections": len(sections),
                    "chunks": len(chunks),
                },
            },
        )
    )
    document = await session.scalar(
        select(Document).where(
            Document.id == version.document_id,
            Document.tenant_id == version.tenant_id,
        )
    )
    tokenizer_mismatch = any(
        provenance.get("embedding_revision") not in {None, settings.embed_revision}
        for provenance in recorded_provenance
    )
    if tokenizer_mismatch:
        checks.append(
            _check(
                "deterministic_reconstruction",
                "WARN",
                {
                    "comparable": False,
                    "reason": "embedding_tokenizer_revision_mismatch",
                },
            )
        )
    elif source_path.exists() and document is not None:
        try:
            blocks = parse_document(
                source_path,
                document.mime,
                expanded_limit_bytes=settings.upload_max_mb * 1024 * 1024,
            )
            built_sections, sectioned = build_sections(version.id, blocks)
            built_chunks = await chunk_blocks(
                version.id,
                sectioned,
                tokenizer,
                max_tokens=settings.chunk_tokens,
                overlap=settings.chunk_overlap,
            )
            reconstructed_sections = [
                (
                    item.id,
                    item.parent_id,
                    item.ordinal,
                    item.level,
                    item.heading_original,
                    item.heading_lexical,
                    item.page_start,
                    item.page_end,
                    item.path_original,
                    item.path_lexical,
                    item.source_metadata,
                )
                for item in built_sections
            ]
            persisted_sections = [
                (
                    item.id,
                    item.parent_id,
                    item.ordinal,
                    item.level,
                    item.heading_original,
                    item.heading_lexical,
                    item.page_start,
                    item.page_end,
                    item.path_original,
                    item.path_lexical,
                    item.source_metadata,
                )
                for item in sections
            ]
            reconstructed = [
                (
                    item.id,
                    item.content_sha256,
                    item.lexical_sha256,
                    item.text_original,
                    item.text_lexical,
                )
                for item in built_chunks
            ]
            persisted = [
                (
                    item.id,
                    item.content_sha256,
                    item.lexical_sha256,
                    item.text_original,
                    item.text_lexical,
                )
                for item in chunks
            ]
            reconstructed_pages = len({block.page_number for block in blocks})
            deterministic_ok = (
                reconstructed_pages == version.page_count
                and reconstructed_sections == persisted_sections
                and reconstructed == persisted
            )
            checks.append(
                _check(
                    "deterministic_reconstruction",
                    "PASS" if deterministic_ok else "FAIL",
                    {
                        "comparable": True,
                        "embedding_revision": settings.embed_revision,
                        "reconstructed_pages": reconstructed_pages,
                        "reconstructed_chunks": len(built_chunks),
                    },
                )
            )
        except Exception as exc:
            checks.append(
                _check(
                    "deterministic_reconstruction",
                    "WARN",
                    {
                        "comparable": False,
                        "reason": type(exc).__name__,
                        "embedding_revision": settings.embed_revision,
                    },
                )
            )
    points = await point_reader.points_for_version(version.tenant_id, version.id)
    checks.append(_qdrant_check(version, chunks, sections, points))
    status = _overall_status(checks)
    return _version_result(version.id, is_active, status, checks)


def _qdrant_check(
    version: DocumentVersion,
    chunks: list[Chunk],
    sections: list[Section],
    points: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    chunks_by_id = {str(item.id): item for item in chunks}
    sections_by_id = {item.id: item for item in sections}
    point_ids = {_string(item.get("id")) for item in points}
    ids_ok = point_ids == set(chunks_by_id)
    payload_ok = True
    vectors_ok = True
    for point in points:
        point_id = _string(point.get("id"))
        chunk = chunks_by_id.get(point_id)
        payload = point.get("payload")
        vector_names = point.get("vector_names")
        if chunk is None or not isinstance(payload, dict):
            payload_ok = False
            continue
        section = sections_by_id.get(chunk.section_id) if chunk.section_id is not None else None
        expected = {
            "tenant_id": str(version.tenant_id),
            "created_generation_id": version.index_generation_id,
            "document_id": str(version.document_id),
            "document_version_id": str(version.id),
            "section_id": str(chunk.section_id),
            "section_path_original": (section.path_original if section is not None else None),
            "section_path_lexical": (section.path_lexical if section is not None else None),
            "content_sha256": chunk.content_sha256,
            "lexical_sha256": chunk.lexical_sha256,
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "char_start": chunk.char_start,
            "char_end": chunk.char_end,
            "occurrence_index": chunk.occurrence_index,
            "text_original": chunk.text_original,
            "text_lexical": chunk.text_lexical,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            payload_ok = False
        actual_collections = payload.get("collection_ids")
        if not isinstance(actual_collections, list) or not _valid_uuid_strings(actual_collections):
            payload_ok = False
        if not isinstance(vector_names, list) or not {"dense", "sparse"}.issubset(
            set(vector_names)
        ):
            vectors_ok = False
    passed = ids_ok and payload_ok and vectors_ok
    return _check(
        "qdrant_points",
        "PASS" if passed else "FAIL",
        {
            "expected_count": len(chunks),
            "actual_count": len(points),
            "ids_match": ids_ok,
            "payloads_match": payload_ok,
            "dense_and_sparse_present": vectors_ok,
        },
    )


def _referenced_versions(turns: Iterable[HistoricalTurn]) -> set[uuid.UUID]:
    result: set[uuid.UUID] = set()
    for turn in turns:
        for source in (*turn.recorded_sources, *turn.recorded_candidates):
            provenance = source.get("provenance")
            if not isinstance(provenance, dict):
                continue
            raw = provenance.get("document_version_id")
            try:
                if isinstance(raw, str):
                    result.add(uuid.UUID(raw))
            except ValueError:
                continue
    return result


def _recorded_provenance_by_version(
    turns: Iterable[HistoricalTurn],
) -> dict[uuid.UUID, tuple[dict[str, str], ...]]:
    result: dict[uuid.UUID, list[dict[str, str]]] = {}
    for turn in turns:
        versions = _referenced_versions((turn,))
        for version_id in versions:
            if turn.recorded_model_provenance:
                result.setdefault(version_id, []).append(turn.recorded_model_provenance)
    return {key: tuple(value) for key, value in result.items()}


def _version_result(
    version_id: uuid.UUID,
    active: bool,
    status: AuditStatus,
    checks: list[dict[str, Any]],
    *,
    unavailable_by_retention: bool = False,
) -> dict[str, Any]:
    return {
        "version_id": str(version_id),
        "active": active,
        "status": status,
        "unavailable_by_retention": unavailable_by_retention,
        "checks": checks,
    }


def _check(name: str, status: AuditStatus, detail: object) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail}


def _overall_status(checks: Iterable[dict[str, Any]]) -> AuditStatus:
    statuses = {item["status"] for item in checks}
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    return "PASS"


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _string(value: object) -> str:
    return value if isinstance(value, str) else str(value)


def _valid_uuid_strings(values: list[object]) -> bool:
    try:
        return len(values) == len({uuid.UUID(value) for value in values if isinstance(value, str)})
    except ValueError:
        return False
