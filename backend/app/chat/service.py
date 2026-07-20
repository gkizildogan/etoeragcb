from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal
from app.core.idempotency import (
    ClaimState,
    canonical_request_hash,
    claim_idempotency,
    complete_idempotency,
)
from app.models import ChatSession, Message


class IdempotencyConflictError(Exception):
    pass


class IdempotencyInProgressError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PersistedMessage:
    message: Message
    replayed: bool


async def persist_user_message(
    session: AsyncSession,
    *,
    principal: Principal,
    session_id: uuid.UUID,
    content: str,
    client_request_id: uuid.UUID,
    idempotency_key: str,
    ttl_seconds: int,
) -> PersistedMessage:
    request_hash = canonical_request_hash(
        {
            "session_id": str(session_id),
            "content": content,
            "client_request_id": str(client_request_id),
        }
    )
    claim = await claim_idempotency(
        session,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        operation="chat_user_message",
        key=idempotency_key,
        request_hash=request_hash,
        ttl_seconds=ttl_seconds,
    )
    if claim.state is ClaimState.CONFLICT:
        raise IdempotencyConflictError
    if claim.state is ClaimState.IN_PROGRESS:
        raise IdempotencyInProgressError
    if claim.state is ClaimState.REPLAY:
        if claim.resource_id is None:
            raise RuntimeError("completed message claim has no resource")
        replayed = await session.scalar(
            select(Message).where(
                Message.id == claim.resource_id,
                Message.tenant_id == principal.tenant_id,
                Message.user_id == principal.user_id,
            )
        )
        if replayed is None:
            raise RuntimeError("completed message resource no longer exists")
        return PersistedMessage(replayed, replayed=True)

    chat_session = await session.scalar(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.tenant_id == principal.tenant_id,
            ChatSession.user_id == principal.user_id,
            ChatSession.deleted_at.is_(None),
        )
    )
    if chat_session is None:
        raise LookupError("session not found")
    message = Message(
        tenant_id=principal.tenant_id,
        session_id=session_id,
        user_id=principal.user_id,
        role="user",
        content=content,
        meta={},
        client_request_id=client_request_id,
    )
    session.add(message)
    chat_session.updated_at = datetime.now(UTC)
    await session.flush()
    response = {
        "message_id": str(message.id),
        "session_id": str(message.session_id),
        "client_request_id": str(client_request_id),
    }
    await complete_idempotency(
        session,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        operation="chat_user_message",
        key=idempotency_key,
        response=response,
        resource_id=message.id,
    )
    await session.commit()
    return PersistedMessage(message, replayed=False)
