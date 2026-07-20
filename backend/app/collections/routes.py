from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal, get_current_principal, require_tenant_admin
from app.collections.schemas import (
    CollectionCreate,
    CollectionListResponse,
    CollectionMutationResponse,
    CollectionResponse,
    CollectionUpdate,
    RetrievalRevisionResponse,
)
from app.core.db import get_db_session
from app.models import Document, DocumentCollection, KnowledgeCollection, Tenant

router = APIRouter(prefix="/api/collections")


@router.get("", response_model=CollectionListResponse)
async def list_collections(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CollectionListResponse:
    collections = list(
        await session.scalars(
            select(KnowledgeCollection)
            .where(
                KnowledgeCollection.tenant_id == principal.tenant_id,
                KnowledgeCollection.deleted_at.is_(None),
            )
            .order_by(KnowledgeCollection.name, KnowledgeCollection.id)
        )
    )
    revision = await _current_revision(session, principal.tenant_id)
    return CollectionListResponse(
        items=[_collection_response(item) for item in collections],
        retrieval_revision=revision,
    )


@router.post("", response_model=CollectionMutationResponse, status_code=status.HTTP_201_CREATED)
async def create_collection(
    body: CollectionCreate,
    principal: Annotated[Principal, Depends(require_tenant_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CollectionMutationResponse:
    collection = KnowledgeCollection(
        tenant_id=principal.tenant_id,
        name=body.name,
        description=body.description,
        created_by=principal.user_id,
    )
    session.add(collection)
    try:
        await session.flush()
        revision = await _bump_revision(session, principal.tenant_id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Collection name already exists"
        ) from exc
    return CollectionMutationResponse(
        collection=_collection_response(collection), retrieval_revision=revision
    )


@router.patch("/{collection_id}", response_model=CollectionMutationResponse)
async def update_collection(
    collection_id: uuid.UUID,
    body: CollectionUpdate,
    principal: Annotated[Principal, Depends(require_tenant_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CollectionMutationResponse:
    collection = await _active_collection(session, principal.tenant_id, collection_id)
    if not body.model_fields_set:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one field is required",
        )
    if "name" in body.model_fields_set:
        if body.name is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Collection name cannot be null",
            )
        collection.name = body.name
    if "description" in body.model_fields_set:
        collection.description = body.description
    collection.updated_at = datetime.now(UTC)
    try:
        await session.flush()
        revision = await _bump_revision(session, principal.tenant_id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Collection name already exists"
        ) from exc
    return CollectionMutationResponse(
        collection=_collection_response(collection), retrieval_revision=revision
    )


@router.delete("/{collection_id}", response_model=RetrievalRevisionResponse)
async def delete_collection(
    collection_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_tenant_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RetrievalRevisionResponse:
    collection = await _active_collection(session, principal.tenant_id, collection_id)
    collection.deleted_at = datetime.now(UTC)
    collection.updated_at = collection.deleted_at
    await session.execute(
        delete(DocumentCollection).where(
            DocumentCollection.tenant_id == principal.tenant_id,
            DocumentCollection.collection_id == collection.id,
        )
    )
    revision = await _bump_revision(session, principal.tenant_id)
    await session.commit()
    return RetrievalRevisionResponse(retrieval_revision=revision, changed=True)


@router.put("/{collection_id}/documents/{document_id}", response_model=RetrievalRevisionResponse)
async def add_document(
    collection_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_tenant_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RetrievalRevisionResponse:
    await _active_collection(session, principal.tenant_id, collection_id)
    await _active_document(session, principal.tenant_id, document_id)
    values = {
        "document_id": document_id,
        "collection_id": collection_id,
        "tenant_id": principal.tenant_id,
    }
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        result = await session.execute(
            postgresql_insert(DocumentCollection)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["document_id", "collection_id"])
        )
    elif bind.dialect.name == "sqlite":
        result = await session.execute(
            sqlite_insert(DocumentCollection)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["document_id", "collection_id"])
        )
    else:  # pragma: no cover
        raise RuntimeError("unsupported collection database dialect")
    changed = result.rowcount == 1
    revision = (
        await _bump_revision(session, principal.tenant_id)
        if changed
        else await _current_revision(session, principal.tenant_id)
    )
    await session.commit()
    return RetrievalRevisionResponse(retrieval_revision=revision, changed=changed)


@router.delete("/{collection_id}/documents/{document_id}", response_model=RetrievalRevisionResponse)
async def remove_document(
    collection_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_tenant_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RetrievalRevisionResponse:
    await _active_collection(session, principal.tenant_id, collection_id)
    await _active_document(session, principal.tenant_id, document_id)
    result = await session.execute(
        delete(DocumentCollection).where(
            DocumentCollection.tenant_id == principal.tenant_id,
            DocumentCollection.collection_id == collection_id,
            DocumentCollection.document_id == document_id,
        )
    )
    changed = result.rowcount == 1
    revision = (
        await _bump_revision(session, principal.tenant_id)
        if changed
        else await _current_revision(session, principal.tenant_id)
    )
    await session.commit()
    return RetrievalRevisionResponse(retrieval_revision=revision, changed=changed)


async def _active_collection(
    session: AsyncSession, tenant_id: uuid.UUID, collection_id: uuid.UUID
) -> KnowledgeCollection:
    collection = await session.scalar(
        select(KnowledgeCollection).where(
            KnowledgeCollection.id == collection_id,
            KnowledgeCollection.tenant_id == tenant_id,
            KnowledgeCollection.deleted_at.is_(None),
        )
    )
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return collection


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


async def _bump_revision(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    result = await session.execute(
        update(Tenant)
        .where(Tenant.id == tenant_id)
        .values(retrieval_revision=Tenant.retrieval_revision + 1)
        .returning(Tenant.retrieval_revision)
    )
    revision = result.scalar_one_or_none()
    if revision is None:
        raise RuntimeError("tenant disappeared during retrieval revision update")
    return int(revision)


async def _current_revision(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    revision = await session.scalar(select(Tenant.retrieval_revision).where(Tenant.id == tenant_id))
    if revision is None:
        raise RuntimeError("tenant does not exist")
    return revision


def _collection_response(item: KnowledgeCollection) -> CollectionResponse:
    return CollectionResponse(
        id=item.id,
        name=item.name,
        description=item.description,
        created_by=item.created_by,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )
