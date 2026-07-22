from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingest.chunker import BuiltChunk, Tokenizer, chunk_blocks
from app.ingest.hashing import sparse_lexical_vector
from app.ingest.indexer import ChunkIndex, IndexPoint
from app.ingest.parsers import parse_document
from app.ingest.sections import BuiltSection, build_sections
from app.ingest.storage import LocalDocumentStorage
from app.models import (
    Chunk,
    Document,
    DocumentCollection,
    DocumentVersion,
    IndexGeneration,
    IndexGenerationDocument,
    IngestionJob,
    Section,
    Tenant,
)


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


FailureInjector = Callable[[str], None]


class IngestionPipeline:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        storage: LocalDocumentStorage,
        tokenizer: Tokenizer,
        embedder: Embedder,
        index: ChunkIndex,
        *,
        chunk_tokens: int,
        chunk_overlap: int,
        batch_size: int,
        upload_max_bytes: int,
        heartbeat_timeout: int,
        failure_injector: FailureInjector | None = None,
    ) -> None:
        self._factory = session_factory
        self._storage = storage
        self._tokenizer = tokenizer
        self._embedder = embedder
        self._index = index
        self._chunk_tokens = chunk_tokens
        self._chunk_overlap = chunk_overlap
        self._batch_size = batch_size
        self._expanded_limit = upload_max_bytes
        self._heartbeat_timeout = heartbeat_timeout
        self._failure_injector = failure_injector

    async def process(self, job_id: uuid.UUID) -> None:
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            started = await self._start(job_id)
            if started is None:
                return
            job, version, document = started
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(job.id))
            source_path = self._storage.resolve(version.storage_key)
            blocks = await asyncio.to_thread(
                parse_document,
                source_path,
                document.mime,
                expanded_limit_bytes=self._expanded_limit,
            )
            built_sections, sectioned_blocks = build_sections(version.id, blocks)
            built_chunks = await chunk_blocks(
                version.id,
                sectioned_blocks,
                self._tokenizer,
                max_tokens=self._chunk_tokens,
                overlap=self._chunk_overlap,
            )
            generation_id, collection_ids = await self._persist_staged_content(
                job.id,
                version,
                built_sections,
                built_chunks,
                page_count=len({block.page_number for block in blocks}),
            )
            self._inject("after_parse")
            await self._index.prepare()
            await self._index.delete_versions(tenant_id=version.tenant_id, version_ids=[version.id])
            section_by_id = {section.id: section for section in built_sections}
            for start in range(0, len(built_chunks), self._batch_size):
                batch = built_chunks[start : start + self._batch_size]
                embeddings = await self._embedder.embed([chunk.text_original for chunk in batch])
                if len(embeddings) != len(batch):
                    raise RuntimeError("embedding count mismatch")
                points: list[IndexPoint] = []
                for chunk, dense in zip(batch, embeddings, strict=True):
                    section = section_by_id[chunk.section_id]
                    sparse = sparse_lexical_vector(chunk.text_lexical)
                    points.append(
                        IndexPoint(
                            id=chunk.id,
                            dense=dense,
                            sparse_indices=sparse.indices,
                            sparse_values=sparse.values,
                            payload={
                                "tenant_id": str(version.tenant_id),
                                "created_generation_id": generation_id,
                                "document_id": str(version.document_id),
                                "document_version_id": str(version.id),
                                "collection_ids": [str(item) for item in collection_ids],
                                "section_id": str(section.id),
                                "section_path_original": section.path_original,
                                "section_path_lexical": section.path_lexical,
                                "page_start": chunk.page_start,
                                "page_end": chunk.page_end,
                                "char_start": chunk.char_start,
                                "char_end": chunk.char_end,
                                "occurrence_index": chunk.occurrence_index,
                                "content_sha256": chunk.content_sha256,
                                "lexical_sha256": chunk.lexical_sha256,
                                "text_original": chunk.text_original,
                                "text_lexical": chunk.text_lexical,
                            },
                        )
                    )
                await self._index.upsert(points)
                await self._heartbeat(job.id)
            await self._index.validate_version(
                tenant_id=version.tenant_id,
                version_id=version.id,
                expected_count=len(built_chunks),
                sample_id=built_chunks[0].id,
            )
            await self._mark_ready(job.id, version.id)
            self._inject("before_activation")
            await self._activate(job.id, version.id, generation_id)
        except Exception as exc:
            await self._mark_failed(job_id, exc)
            raise
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task

    async def _start(
        self, job_id: uuid.UUID
    ) -> tuple[IngestionJob, DocumentVersion, Document] | None:
        async with self._factory() as session:
            preliminary_job = await session.scalar(
                select(IngestionJob).where(IngestionJob.id == job_id)
            )
            if preliminary_job is None:
                raise RuntimeError("ingestion job does not exist")
            preliminary_version = await session.scalar(
                select(DocumentVersion).where(
                    DocumentVersion.id == preliminary_job.document_version_id,
                    DocumentVersion.tenant_id == preliminary_job.tenant_id,
                )
            )
            if preliminary_version is None:
                raise RuntimeError("document version does not exist")
            await session.scalar(
                select(Tenant).where(Tenant.id == preliminary_version.tenant_id).with_for_update()
            )
            document = await session.scalar(
                select(Document)
                .where(
                    Document.id == preliminary_version.document_id,
                    Document.tenant_id == preliminary_version.tenant_id,
                )
                .with_for_update()
            )
            if document is None:
                raise RuntimeError("document does not exist")
            version = await session.scalar(
                select(DocumentVersion)
                .where(DocumentVersion.id == preliminary_version.id)
                .with_for_update()
            )
            job = await session.scalar(
                select(IngestionJob).where(IngestionJob.id == job_id).with_for_update()
            )
            if version is None or job is None:
                raise RuntimeError("ingestion state disappeared")
            if job.status == "succeeded":
                return None
            heartbeat_at = job.heartbeat_at
            if heartbeat_at is not None and heartbeat_at.tzinfo is None:
                heartbeat_at = heartbeat_at.replace(tzinfo=UTC)
            if (
                job.status == "processing"
                and heartbeat_at is not None
                and heartbeat_at >= datetime.now(UTC) - timedelta(seconds=self._heartbeat_timeout)
            ):
                return None
            job.status = "processing"
            job.attempt += 1
            job.heartbeat_at = datetime.now(UTC)
            job.error = None
            version.status = "processing"
            version.error_code = None
            version.error_detail = None
            await session.commit()
            return job, version, document

    async def _persist_staged_content(
        self,
        job_id: uuid.UUID,
        version: DocumentVersion,
        sections: list[BuiltSection],
        chunks: list[BuiltChunk],
        *,
        page_count: int,
    ) -> tuple[int, list[uuid.UUID]]:
        async with self._factory() as session:
            tenant = await session.scalar(
                select(Tenant).where(Tenant.id == version.tenant_id).with_for_update()
            )
            current_version = await session.scalar(
                select(DocumentVersion).where(DocumentVersion.id == version.id).with_for_update()
            )
            job = await session.scalar(
                select(IngestionJob).where(IngestionJob.id == job_id).with_for_update()
            )
            if tenant is None or current_version is None or job is None:
                raise RuntimeError("ingestion state disappeared")
            await session.execute(
                delete(Chunk).where(Chunk.document_version_id == current_version.id)
            )
            await session.execute(
                delete(Section).where(Section.document_version_id == current_version.id)
            )
            prior_generation = await session.scalar(
                select(IndexGeneration)
                .where(
                    IndexGeneration.changed_document_version_id == current_version.id,
                    IndexGeneration.status == "preparing",
                )
                .order_by(IndexGeneration.id.desc())
            )
            if prior_generation is None:
                prior_generation = IndexGeneration(
                    tenant_id=current_version.tenant_id,
                    reason="document_upload",
                    changed_document_version_id=current_version.id,
                    parent_generation_id=tenant.active_index_generation_id,
                    status="preparing",
                    retrieval_revision=tenant.retrieval_revision + 1,
                )
                session.add(prior_generation)
                await session.flush()
            else:
                await session.execute(
                    delete(IndexGenerationDocument).where(
                        IndexGenerationDocument.generation_id == prior_generation.id
                    )
                )
                prior_generation.parent_generation_id = tenant.active_index_generation_id
                prior_generation.retrieval_revision = tenant.retrieval_revision + 1
            await _copy_manifest(
                session,
                source_generation_id=tenant.active_index_generation_id,
                target_generation_id=prior_generation.id,
                tenant_id=tenant.id,
                exclude_document_id=current_version.document_id,
            )
            session.add(
                IndexGenerationDocument(
                    generation_id=prior_generation.id,
                    tenant_id=current_version.tenant_id,
                    document_id=current_version.document_id,
                    document_version_id=current_version.id,
                )
            )
            for section in sections:
                session.add(
                    Section(
                        id=section.id,
                        tenant_id=current_version.tenant_id,
                        document_id=current_version.document_id,
                        document_version_id=current_version.id,
                        parent_id=section.parent_id,
                        ordinal=section.ordinal,
                        level=section.level,
                        heading_original=section.heading_original,
                        heading_lexical=section.heading_lexical,
                        page_start=section.page_start,
                        page_end=section.page_end,
                        path_original=section.path_original,
                        path_lexical=section.path_lexical,
                        source_metadata=section.source_metadata,
                    )
                )
            for chunk in chunks:
                session.add(
                    Chunk(
                        id=chunk.id,
                        tenant_id=current_version.tenant_id,
                        document_id=current_version.document_id,
                        document_version_id=current_version.id,
                        section_id=chunk.section_id,
                        occurrence_index=chunk.occurrence_index,
                        chunk_index=chunk.chunk_index,
                        page_start=chunk.page_start,
                        page_end=chunk.page_end,
                        char_start=chunk.char_start,
                        char_end=chunk.char_end,
                        content_sha256=chunk.content_sha256,
                        lexical_sha256=chunk.lexical_sha256,
                        token_count=chunk.token_count,
                        text_original=chunk.text_original,
                        text_lexical=chunk.text_lexical,
                    )
                )
            current_version.status = "processing"
            current_version.page_count = page_count
            current_version.section_count = len(sections)
            current_version.chunk_count = len(chunks)
            current_version.index_generation_id = prior_generation.id
            job.heartbeat_at = datetime.now(UTC)
            collection_ids = list(
                await session.scalars(
                    select(DocumentCollection.collection_id).where(
                        DocumentCollection.document_id == current_version.document_id,
                        DocumentCollection.tenant_id == current_version.tenant_id,
                    )
                )
            )
            await session.commit()
            return prior_generation.id, collection_ids

    async def _heartbeat(self, job_id: uuid.UUID) -> None:
        async with self._factory() as session:
            job = await session.scalar(select(IngestionJob).where(IngestionJob.id == job_id))
            if job is not None and job.status == "processing":
                job.heartbeat_at = datetime.now(UTC)
                await session.commit()

    async def _heartbeat_loop(self, job_id: uuid.UUID) -> None:
        interval = max(5, self._heartbeat_timeout // 3)
        while True:
            await asyncio.sleep(interval)
            await self._heartbeat(job_id)

    async def _mark_ready(self, job_id: uuid.UUID, version_id: uuid.UUID) -> None:
        async with self._factory() as session:
            version = await session.scalar(
                select(DocumentVersion).where(DocumentVersion.id == version_id).with_for_update()
            )
            job = await session.scalar(
                select(IngestionJob).where(IngestionJob.id == job_id).with_for_update()
            )
            if version is None or job is None:
                raise RuntimeError("ingestion state disappeared before validation")
            if job.status == "succeeded":
                return
            version.status = "ready"
            job.heartbeat_at = datetime.now(UTC)
            await session.commit()

    async def _activate(self, job_id: uuid.UUID, version_id: uuid.UUID, generation_id: int) -> None:
        now = datetime.now(UTC)
        async with self._factory() as session:
            preliminary_version = await session.scalar(
                select(DocumentVersion).where(DocumentVersion.id == version_id)
            )
            if preliminary_version is None:
                raise RuntimeError("ingestion state disappeared before activation")
            tenant = await session.scalar(
                select(Tenant).where(Tenant.id == preliminary_version.tenant_id).with_for_update()
            )
            document = await session.scalar(
                select(Document)
                .where(
                    Document.id == preliminary_version.document_id,
                    Document.tenant_id == preliminary_version.tenant_id,
                )
                .with_for_update()
            )
            version = await session.scalar(
                select(DocumentVersion).where(DocumentVersion.id == version_id).with_for_update()
            )
            generation = await session.scalar(
                select(IndexGeneration).where(IndexGeneration.id == generation_id).with_for_update()
            )
            job = await session.scalar(
                select(IngestionJob).where(IngestionJob.id == job_id).with_for_update()
            )
            if version is None or generation is None or job is None:
                raise RuntimeError("ingestion state disappeared before activation")
            if job.status == "succeeded":
                return
            if tenant is None or document is None or document.deleted_at is not None:
                raise RuntimeError("document was deleted before activation")
            if document.active_version_id is not None and document.active_version_id != version.id:
                active_version_number = await session.scalar(
                    select(DocumentVersion.version).where(
                        DocumentVersion.id == document.active_version_id
                    )
                )
                if active_version_number is not None and active_version_number > version.version:
                    raise RuntimeError("a newer document version is already active")
            if generation.parent_generation_id != tenant.active_index_generation_id:
                await session.execute(
                    delete(IndexGenerationDocument).where(
                        IndexGenerationDocument.generation_id == generation.id
                    )
                )
                await _copy_manifest(
                    session,
                    source_generation_id=tenant.active_index_generation_id,
                    target_generation_id=generation.id,
                    tenant_id=tenant.id,
                    exclude_document_id=document.id,
                )
                session.add(
                    IndexGenerationDocument(
                        generation_id=generation.id,
                        tenant_id=tenant.id,
                        document_id=document.id,
                        document_version_id=version.id,
                    )
                )
                generation.parent_generation_id = tenant.active_index_generation_id
            previous_version_id = document.active_version_id
            if previous_version_id is not None and previous_version_id != version.id:
                previous = await session.scalar(
                    select(DocumentVersion).where(DocumentVersion.id == previous_version_id)
                )
                if previous is not None:
                    previous.status = "superseded"
            revision = tenant.retrieval_revision + 1
            version.status = "active"
            version.activated_at = now
            generation.status = "active"
            generation.retrieval_revision = revision
            generation.activated_at = now
            document.active_version_id = version.id
            tenant.active_index_generation_id = generation.id
            tenant.retrieval_revision = revision
            job.status = "succeeded"
            job.heartbeat_at = now
            job.error = None
            await session.commit()

    async def _mark_failed(self, job_id: uuid.UUID, exc: Exception) -> None:
        async with self._factory() as session:
            preliminary_job = await session.scalar(
                select(IngestionJob).where(IngestionJob.id == job_id)
            )
            if preliminary_job is None or preliminary_job.status == "succeeded":
                return
            version = await session.scalar(
                select(DocumentVersion)
                .where(DocumentVersion.id == preliminary_job.document_version_id)
                .with_for_update()
            )
            job = await session.scalar(
                select(IngestionJob).where(IngestionJob.id == job_id).with_for_update()
            )
            if job is None or job.status == "succeeded":
                return
            detail = str(exc).strip()[:1000] or type(exc).__name__
            job.status = "failed"
            job.error = detail
            job.heartbeat_at = datetime.now(UTC)
            if version is not None and version.status != "active":
                version.status = "failed"
                version.error_code = "INGESTION_FAILED"
                version.error_detail = detail
                if version.index_generation_id is not None:
                    generation = await session.scalar(
                        select(IndexGeneration).where(
                            IndexGeneration.id == version.index_generation_id
                        )
                    )
                    if generation is not None and generation.status == "preparing":
                        generation.status = "failed"
            await session.commit()

    def _inject(self, stage: str) -> None:
        if self._failure_injector is not None:
            self._failure_injector(stage)


async def _copy_manifest(
    session: AsyncSession,
    *,
    source_generation_id: int | None,
    target_generation_id: int,
    tenant_id: uuid.UUID,
    exclude_document_id: uuid.UUID,
) -> None:
    if source_generation_id is None:
        return
    rows = (
        await session.execute(
            select(
                IndexGenerationDocument.document_id,
                IndexGenerationDocument.document_version_id,
            ).where(
                IndexGenerationDocument.generation_id == source_generation_id,
                IndexGenerationDocument.tenant_id == tenant_id,
                IndexGenerationDocument.document_id != exclude_document_id,
            )
        )
    ).all()
    session.add_all(
        [
            IndexGenerationDocument(
                generation_id=target_generation_id,
                tenant_id=tenant_id,
                document_id=document_id,
                document_version_id=document_version_id,
            )
            for document_id, document_version_id in rows
        ]
    )
