from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal, get_current_principal, load_owned_session
from app.core.db import get_db_session
from app.core.pagination import CursorCodec, CursorPosition, InvalidCursorError
from app.models import ChatSession, Chunk, Document, DocumentVersion, Feedback, Message
from app.sessions.schemas import (
    CitationPreviewResponse,
    FeedbackRequest,
    FeedbackResponse,
    MessagePage,
    MessageResponse,
    SessionCreate,
    SessionPage,
    SessionResponse,
)

router = APIRouter(prefix="/api")
SOURCE_ID_RE = re.compile(r"^S[1-9][0-9]*$")


@router.get("/sessions", response_model=SessionPage)
async def list_sessions(
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> SessionPage:
    statement = select(ChatSession).where(
        ChatSession.tenant_id == principal.tenant_id,
        ChatSession.user_id == principal.user_id,
        ChatSession.deleted_at.is_(None),
    )
    if cursor is not None:
        position = _decode_cursor(request.app.state.cursor_codec, cursor, "sessions")
        statement = statement.where(
            or_(
                ChatSession.updated_at < position.occurred_at,
                and_(
                    ChatSession.updated_at == position.occurred_at,
                    ChatSession.id < position.resource_id,
                ),
            )
        )
    rows = list(
        await session.scalars(
            statement.order_by(ChatSession.updated_at.desc(), ChatSession.id.desc()).limit(
                limit + 1
            )
        )
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    return SessionPage(
        items=[_session_response(item) for item in items],
        next_cursor=(
            request.app.state.cursor_codec.encode(
                kind="sessions",
                occurred_at=_as_utc(items[-1].updated_at),
                resource_id=items[-1].id,
            )
            if has_more
            else None
        ),
    )


@router.post("/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: SessionCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> SessionResponse:
    chat_session = ChatSession(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        title=body.title,
    )
    session.add(chat_session)
    await session.commit()
    return _session_response(chat_session)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
    owned = await load_owned_session(session_id, principal=principal, session=session)
    owned.deleted_at = datetime.now(UTC)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions/{session_id}/messages", response_model=MessagePage)
async def list_messages(
    session_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MessagePage:
    await load_owned_session(session_id, principal=principal, session=session)
    statement = select(Message).where(
        Message.tenant_id == principal.tenant_id,
        Message.user_id == principal.user_id,
        Message.session_id == session_id,
    )
    if cursor is not None:
        position = _decode_cursor(request.app.state.cursor_codec, cursor, "messages")
        statement = statement.where(
            or_(
                Message.created_at > position.occurred_at,
                and_(
                    Message.created_at == position.occurred_at,
                    Message.id > position.resource_id,
                ),
            )
        )
    rows = list(
        await session.scalars(statement.order_by(Message.created_at, Message.id).limit(limit + 1))
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    return MessagePage(
        items=[_message_response(item) for item in items],
        next_cursor=(
            request.app.state.cursor_codec.encode(
                kind="messages",
                occurred_at=_as_utc(items[-1].created_at),
                resource_id=items[-1].id,
            )
            if has_more
            else None
        ),
    )


@router.post("/messages/{message_id}/feedback", response_model=FeedbackResponse)
async def submit_feedback(
    message_id: uuid.UUID,
    body: FeedbackRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> FeedbackResponse:
    message = await session.scalar(
        select(Message).where(
            Message.id == message_id,
            Message.tenant_id == principal.tenant_id,
            Message.user_id == principal.user_id,
            Message.role == "assistant",
        )
    )
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    feedback = await session.scalar(
        select(Feedback).where(
            Feedback.message_id == message.id,
            Feedback.user_id == principal.user_id,
        )
    )
    if feedback is None:
        feedback = Feedback(
            tenant_id=principal.tenant_id,
            message_id=message.id,
            user_id=principal.user_id,
            rating=body.rating,
            comment=body.comment,
        )
        session.add(feedback)
    else:
        feedback.rating = body.rating
        feedback.comment = body.comment
    await session.commit()
    return FeedbackResponse(
        id=feedback.id,
        message_id=feedback.message_id,
        rating=cast(Literal[-1, 1], feedback.rating),
        comment=feedback.comment,
        created_at=feedback.created_at,
    )


@router.get(
    "/messages/{message_id}/citations/{source_id}/preview",
    response_model=CitationPreviewResponse,
)
async def preview_citation(
    message_id: uuid.UUID,
    source_id: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
    not_found = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if SOURCE_ID_RE.fullmatch(source_id) is None:
        raise not_found

    message = await session.scalar(
        select(Message).where(
            Message.id == message_id,
            Message.tenant_id == principal.tenant_id,
            Message.user_id == principal.user_id,
            Message.role == "assistant",
        )
    )
    if message is None:
        raise not_found

    resolved = _resolve_document_source(message.meta, source_id)
    if resolved is None:
        raise not_found
    chunk_id, document_id, document_version_id, content_sha256, section_id, chunk_index = resolved

    statement = (
        select(Chunk, Document, DocumentVersion)
        .join(
            Document,
            and_(
                Document.id == Chunk.document_id,
                Document.tenant_id == Chunk.tenant_id,
            ),
        )
        .join(
            DocumentVersion,
            and_(
                DocumentVersion.id == Chunk.document_version_id,
                DocumentVersion.document_id == Chunk.document_id,
                DocumentVersion.tenant_id == Chunk.tenant_id,
            ),
        )
        .where(
            Chunk.id == chunk_id,
            Chunk.tenant_id == principal.tenant_id,
            Chunk.document_id == document_id,
            Chunk.document_version_id == document_version_id,
            Document.deleted_at.is_(None),
            DocumentVersion.status.in_(("ready", "active", "superseded")),
            DocumentVersion.garbage_collected_at.is_(None),
        )
    )
    row = (await session.execute(statement)).one_or_none()
    if row is None:
        raise not_found
    chunk, document, _version = row
    if (
        chunk.content_sha256 != content_sha256
        or (section_id is not None and chunk.section_id != section_id)
        or (chunk_index is not None and chunk.chunk_index != chunk_index)
    ):
        raise not_found

    payload = CitationPreviewResponse(
        message_id=message.id,
        source_id=source_id,
        title=document.title,
        source_filename=document.source_filename,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        text=chunk.text_original,
    )
    return Response(
        content=payload.model_dump_json(),
        media_type="application/json",
        headers={"Cache-Control": "private, no-store"},
    )


def _session_response(item: ChatSession) -> SessionResponse:
    return SessionResponse(
        id=item.id,
        title=item.title,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _message_response(item: Message) -> MessageResponse:
    return MessageResponse(
        id=item.id,
        role=item.role,
        content=item.content,
        meta=item.meta,
        client_request_id=item.client_request_id,
        created_at=item.created_at,
    )


def _decode_cursor(codec: CursorCodec, cursor: str, kind: str) -> CursorPosition:
    try:
        return codec.decode(cursor, expected_kind=kind)
    except InvalidCursorError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid cursor"
        ) from exc


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _resolve_document_source(
    metadata: object,
    source_id: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str, uuid.UUID | None, int | None] | None:
    if not isinstance(metadata, dict):
        return None
    citations = metadata.get("citations")
    retrieval = metadata.get("retrieval")
    if not isinstance(citations, dict) or not isinstance(retrieval, dict):
        return None
    citation = citations.get(f"[{source_id}]")
    sources = retrieval.get("sources")
    if (
        not isinstance(citation, dict)
        or citation.get("source_id") != source_id
        or citation.get("source_type") != "document"
        or not isinstance(sources, list)
    ):
        return None
    matches = [
        item
        for item in sources
        if isinstance(item, dict)
        and item.get("source_id") == source_id
        and item.get("source_type") == "document"
    ]
    if len(matches) != 1:
        return None
    source = matches[0]
    provenance = source.get("provenance")
    content_sha256 = source.get("content_sha256")
    if not isinstance(provenance, dict) or not isinstance(content_sha256, str):
        return None
    try:
        chunk_id = uuid.UUID(str(source.get("candidate_id")))
        document_id = uuid.UUID(str(provenance.get("document_id")))
        document_version_id = uuid.UUID(str(provenance.get("document_version_id")))
        citation_document_id = uuid.UUID(str(citation.get("document_id")))
        citation_version_id = uuid.UUID(str(citation.get("document_version_id")))
        raw_section_id = provenance.get("section_id")
        section_id = uuid.UUID(str(raw_section_id)) if raw_section_id is not None else None
    except ValueError:
        return None
    if document_id != citation_document_id or document_version_id != citation_version_id:
        return None
    chunk_index = provenance.get("chunk_index")
    if chunk_index is not None and (
        not isinstance(chunk_index, int) or isinstance(chunk_index, bool) or chunk_index < 0
    ):
        return None
    if re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None:
        return None
    return (
        chunk_id,
        document_id,
        document_version_id,
        content_sha256,
        section_id,
        chunk_index,
    )
