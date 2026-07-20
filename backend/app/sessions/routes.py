from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal, get_current_principal, load_owned_session
from app.core.db import get_db_session
from app.core.pagination import CursorCodec, CursorPosition, InvalidCursorError
from app.models import ChatSession, Feedback, Message
from app.sessions.schemas import (
    FeedbackRequest,
    FeedbackResponse,
    MessagePage,
    MessageResponse,
    SessionCreate,
    SessionPage,
    SessionResponse,
)

router = APIRouter(prefix="/api")


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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid cursor"
        ) from exc


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
