from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal, get_current_principal, require_tenant_admin
from app.auth.rate_limit import RateLimiter
from app.core.db import get_db_session
from app.core.idempotency import (
    ClaimState,
    canonical_request_hash,
    claim_idempotency,
    complete_idempotency,
)
from app.documents.schemas import (
    DeleteDocumentResponse,
    DocumentListResponse,
    DocumentResponse,
    DocumentVersionResponse,
    UploadAccepted,
)
from app.ingest.reconcile import tombstone_document
from app.ingest.storage import LocalDocumentStorage, StagedUpload, UploadValidationError
from app.models import (
    Document,
    DocumentCollection,
    DocumentVersion,
    IngestionJob,
    KnowledgeCollection,
    Tenant,
)

router = APIRouter(prefix="/api/documents")


@router.post("", response_model=UploadAccepted, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    request: Request,
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form(min_length=1, max_length=300)],
    principal: Annotated[Principal, Depends(require_tenant_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    document_id: Annotated[uuid.UUID | None, Form()] = None,
    collection_ids_json: Annotated[str, Form(max_length=10_000)] = "[]",
) -> UploadAccepted:
    settings = request.app.state.settings
    normalized_title = title.strip()
    if not normalized_title:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Document title cannot be blank",
        )
    limiter: RateLimiter = request.app.state.rate_limiter
    decision = await limiter.check(
        "upload",
        {"tenant": str(principal.tenant_id), "user": str(principal.user_id)},
        settings.upload_rate_limits,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Upload rate limit exceeded",
            headers={"Retry-After": str(decision.retry_after)},
        )
    collection_ids = _parse_collection_ids(collection_ids_json)
    storage: LocalDocumentStorage = request.app.state.document_storage
    try:
        staged = await storage.stage_upload(
            file,
            max_bytes=settings.upload_max_mb * 1024 * 1024,
            allowed_mime=set(settings.allowed_mime),
        )
    except UploadValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    request_hash = canonical_request_hash(
        {
            "title": normalized_title,
            "document_id": str(document_id) if document_id else None,
            "collection_ids": sorted(str(item) for item in collection_ids),
            "filename": staged.source_filename,
            "mime": staged.mime,
            "size": staged.size_bytes,
            "sha256": staged.file_sha256,
        }
    )
    storage_key: str | None = None
    committed = False
    try:
        tenant = await session.scalar(
            select(Tenant).where(Tenant.id == principal.tenant_id).with_for_update()
        )
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        claim = await claim_idempotency(
            session,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            operation="document_upload",
            key=idempotency_key,
            request_hash=request_hash,
            ttl_seconds=settings.idempotency_ttl,
        )
        if claim.state == ClaimState.REPLAY:
            await session.rollback()
            storage.discard(staged)
            return UploadAccepted.model_validate(claim.response)
        if claim.state == ClaimState.CONFLICT:
            await session.rollback()
            storage.discard(staged)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency-Key was already used for a different upload",
            )
        if claim.state == ClaimState.IN_PROGRESS:
            await session.rollback()
            storage.discard(staged)
            raise HTTPException(
                status_code=status.HTTP_425_TOO_EARLY,
                detail="An identical upload is still being accepted",
                headers={"Retry-After": "2"},
            )
        await _validate_quota(
            session,
            tenant_id=principal.tenant_id,
            new_size=staged.size_bytes,
            quota_bytes=settings.tenant_upload_quota_mb * 1024 * 1024,
        )
        document = await _load_or_create_document(
            session,
            principal=principal,
            document_id=document_id,
            title=normalized_title,
            staged=staged,
        )
        if document_id is None:
            await _validate_collections(session, principal.tenant_id, collection_ids)
        elif collection_ids:
            current_collections = set(
                await session.scalars(
                    select(DocumentCollection.collection_id).where(
                        DocumentCollection.document_id == document.id,
                        DocumentCollection.tenant_id == principal.tenant_id,
                    )
                )
            )
            if set(collection_ids) != current_collections:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Change collection membership separately before uploading a new version",
                )
        next_version = (
            await session.scalar(
                select(func.max(DocumentVersion.version)).where(
                    DocumentVersion.document_id == document.id
                )
            )
            or 0
        ) + 1
        version = DocumentVersion(
            id=uuid.uuid4(),
            tenant_id=principal.tenant_id,
            document_id=document.id,
            version=next_version,
            file_sha256=staged.file_sha256,
            file_size_bytes=staged.size_bytes,
            storage_key="pending",
            status="staged",
        )
        storage_key = storage.commit(
            staged,
            tenant_id=principal.tenant_id,
            document_id=document.id,
            document_version_id=version.id,
        )
        version.storage_key = storage_key
        job = IngestionJob(
            tenant_id=principal.tenant_id,
            document_version_id=version.id,
            status="staged",
        )
        session.add_all([version, job])
        if document_id is None:
            session.add_all(
                [
                    DocumentCollection(
                        document_id=document.id,
                        collection_id=collection_id,
                        tenant_id=principal.tenant_id,
                    )
                    for collection_id in collection_ids
                ]
            )
        await session.flush()
        response = UploadAccepted(
            document_id=document.id,
            document_version_id=version.id,
            version=version.version,
            job_id=job.id,
            status="staged",
        )
        await complete_idempotency(
            session,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            operation="document_upload",
            key=idempotency_key,
            response=response.model_dump(mode="json"),
            resource_id=document.id,
        )
        await session.commit()
        committed = True
    except HTTPException:
        if not committed:
            await session.rollback()
            if storage_key is not None:
                storage.delete(storage_key)
            else:
                storage.discard(staged)
        raise
    except (IntegrityError, OSError, ValueError) as exc:
        await session.rollback()
        if storage_key is not None:
            storage.delete(storage_key)
        else:
            storage.discard(staged)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Upload could not be accepted",
        ) from exc
    except Exception:
        await session.rollback()
        if storage_key is not None:
            storage.delete(storage_key)
        else:
            storage.discard(staged)
        raise
    await _enqueue_durably(request, session, response.job_id)
    return response


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DocumentListResponse:
    documents = list(
        await session.scalars(
            select(Document)
            .where(
                Document.tenant_id == principal.tenant_id,
                Document.deleted_at.is_(None),
            )
            .order_by(Document.created_at.desc(), Document.id.desc())
        )
    )
    tenant = await session.scalar(select(Tenant).where(Tenant.id == principal.tenant_id))
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return DocumentListResponse(
        items=[await _document_response(session, document) for document in documents],
        active_index_generation_id=tenant.active_index_generation_id,
        retrieval_revision=tenant.retrieval_revision,
    )


@router.post(
    "/{document_id}/reindex",
    response_model=UploadAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reindex_document(
    document_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_tenant_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> UploadAccepted:
    settings = request.app.state.settings
    limiter: RateLimiter = request.app.state.rate_limiter
    decision = await limiter.check(
        "reindex",
        {"tenant": str(principal.tenant_id), "user": str(principal.user_id)},
        settings.upload_rate_limits,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Reindex rate limit exceeded",
            headers={"Retry-After": str(decision.retry_after)},
        )
    tenant = await session.scalar(
        select(Tenant).where(Tenant.id == principal.tenant_id).with_for_update()
    )
    document = await session.scalar(
        select(Document)
        .where(
            Document.id == document_id,
            Document.tenant_id == principal.tenant_id,
            Document.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if tenant is None or document is None or document.active_version_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    source = await session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.id == document.active_version_id,
            DocumentVersion.tenant_id == principal.tenant_id,
        )
    )
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Active version is missing"
        )
    request_hash = canonical_request_hash({"document_id": str(document.id)})
    claim = await claim_idempotency(
        session,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        operation="document_reindex",
        key=idempotency_key,
        request_hash=request_hash,
        ttl_seconds=settings.idempotency_ttl,
    )
    if claim.state == ClaimState.REPLAY:
        await session.rollback()
        return UploadAccepted.model_validate(claim.response)
    if claim.state == ClaimState.CONFLICT:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key was already used for a different reindex request",
        )
    if claim.state == ClaimState.IN_PROGRESS:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_425_TOO_EARLY,
            detail="This reindex request is still being accepted",
            headers={"Retry-After": "2"},
        )
    await _validate_quota(
        session,
        tenant_id=principal.tenant_id,
        new_size=source.file_size_bytes,
        quota_bytes=settings.tenant_upload_quota_mb * 1024 * 1024,
    )
    next_version = (
        await session.scalar(
            select(func.max(DocumentVersion.version)).where(
                DocumentVersion.document_id == document.id
            )
        )
        or 0
    ) + 1
    version = DocumentVersion(
        id=uuid.uuid4(),
        tenant_id=principal.tenant_id,
        document_id=document.id,
        version=next_version,
        file_sha256=source.file_sha256,
        file_size_bytes=source.file_size_bytes,
        storage_key="pending",
        status="staged",
    )
    storage: LocalDocumentStorage = request.app.state.document_storage
    storage_key: str | None = None
    try:
        storage_key = storage.clone(
            source.storage_key,
            tenant_id=principal.tenant_id,
            document_id=document.id,
            document_version_id=version.id,
        )
        version.storage_key = storage_key
        job = IngestionJob(
            tenant_id=principal.tenant_id,
            document_version_id=version.id,
            status="staged",
        )
        session.add_all([version, job])
        await session.flush()
        response = UploadAccepted(
            document_id=document.id,
            document_version_id=version.id,
            version=version.version,
            job_id=job.id,
            status="staged",
        )
        await complete_idempotency(
            session,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            operation="document_reindex",
            key=idempotency_key,
            response=response.model_dump(mode="json"),
            resource_id=document.id,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        if storage_key is not None:
            storage.delete(storage_key)
        raise
    await _enqueue_durably(request, session, response.job_id)
    return response


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DocumentResponse:
    document = await _active_document(session, principal.tenant_id, document_id)
    return await _document_response(session, document)


@router.delete("/{document_id}", response_model=DeleteDocumentResponse)
async def delete_document(
    document_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_tenant_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DeleteDocumentResponse:
    try:
        revision = await tombstone_document(
            session, tenant_id=principal.tenant_id, document_id=document_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found") from exc
    return DeleteDocumentResponse(retrieval_revision=revision)


async def _load_or_create_document(
    session: AsyncSession,
    *,
    principal: Principal,
    document_id: uuid.UUID | None,
    title: str,
    staged: StagedUpload,
) -> Document:
    if document_id is None:
        document = Document(
            tenant_id=principal.tenant_id,
            title=title,
            source_filename=staged.source_filename,
            mime=staged.mime,
            created_by=principal.user_id,
        )
        session.add(document)
        await session.flush()
        return document
    existing_document = await session.scalar(
        select(Document)
        .where(
            Document.id == document_id,
            Document.tenant_id == principal.tenant_id,
            Document.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if existing_document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if existing_document.mime != staged.mime:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A new version must use the original document MIME type",
        )
    if existing_document.title != title:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Change the document title separately before uploading a new version",
        )
    return existing_document


async def _validate_collections(
    session: AsyncSession, tenant_id: uuid.UUID, collection_ids: list[uuid.UUID]
) -> None:
    if not collection_ids:
        return
    count = await session.scalar(
        select(func.count())
        .select_from(KnowledgeCollection)
        .where(
            KnowledgeCollection.tenant_id == tenant_id,
            KnowledgeCollection.id.in_(collection_ids),
            KnowledgeCollection.deleted_at.is_(None),
        )
    )
    if count != len(collection_ids):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


async def _validate_quota(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    new_size: int,
    quota_bytes: int,
) -> None:
    used = (
        await session.scalar(
            select(func.coalesce(func.sum(DocumentVersion.file_size_bytes), 0)).where(
                DocumentVersion.tenant_id == tenant_id
            )
        )
        or 0
    )
    if int(used) + new_size > quota_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Tenant document quota exceeded",
        )


async def _enqueue_durably(request: Request, session: AsyncSession, job_id: uuid.UUID) -> None:
    try:
        arq_job_id = await request.app.state.ingestion_queue.enqueue(job_id)
        if arq_job_id is None:
            return
        job = await session.scalar(select(IngestionJob).where(IngestionJob.id == job_id))
        if job is not None and job.status == "staged":
            job.status = "queued"
            job.arq_job_id = arq_job_id
            job.heartbeat_at = datetime.now(UTC)
            await session.commit()
    except Exception:
        await session.rollback()


async def _active_document(
    session: AsyncSession, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> Document:
    document = await session.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.tenant_id == tenant_id,
            Document.deleted_at.is_(None),
        )
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return document


async def _document_response(session: AsyncSession, document: Document) -> DocumentResponse:
    versions = list(
        await session.scalars(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document.id)
            .order_by(DocumentVersion.version.desc())
        )
    )
    collection_ids = list(
        await session.scalars(
            select(DocumentCollection.collection_id)
            .where(DocumentCollection.document_id == document.id)
            .order_by(DocumentCollection.collection_id)
        )
    )
    return DocumentResponse(
        id=document.id,
        title=document.title,
        source_filename=document.source_filename,
        mime=document.mime,
        active_version_id=document.active_version_id,
        collection_ids=collection_ids,
        created_at=document.created_at,
        versions=[
            DocumentVersionResponse(
                id=version.id,
                version=version.version,
                status=version.status,
                file_sha256=version.file_sha256,
                file_size_bytes=version.file_size_bytes,
                page_count=version.page_count,
                section_count=version.section_count,
                chunk_count=version.chunk_count,
                error_code=version.error_code,
                error_detail=version.error_detail,
                created_at=version.created_at,
                activated_at=version.activated_at,
            )
            for version in versions
        ],
    )


def _parse_collection_ids(raw: str) -> list[uuid.UUID]:
    try:
        values: Any = json.loads(raw)
        if not isinstance(values, list) or len(values) > 100:
            raise ValueError
        result = [uuid.UUID(value) for value in values]
    except (AttributeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="collection_ids_json must be a JSON array of UUID strings",
        ) from exc
    if len(result) != len(set(result)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="collection IDs must be unique",
        )
    return result
