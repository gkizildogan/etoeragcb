from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

import orjson
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal, get_current_principal
from app.auth.rate_limit import RateLimiter
from app.chat.orchestrator import (
    ChatConflictError,
    ChatCoordinator,
    ChatInProgressError,
    ChatSessionNotFoundError,
)
from app.chat.schemas import AcceptedChat, ChatRequest, StreamEvent
from app.core.db import get_db_session

router = APIRouter(prefix="/api")


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> StreamingResponse:
    settings = request.app.state.settings
    limiter: RateLimiter = request.app.state.rate_limiter
    decision = await limiter.check(
        "chat",
        {"tenant": str(principal.tenant_id), "user": str(principal.user_id)},
        settings.chat_rate_limits,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Chat rate limit exceeded",
            headers={"Retry-After": str(decision.retry_after)},
        )
    coordinator: ChatCoordinator = request.app.state.chat_coordinator
    try:
        accepted = await coordinator.accept(
            session,
            principal=principal,
            request=body,
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except ChatConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency or client request identifier was reused",
        ) from exc
    except ChatInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_425_TOO_EARLY,
            detail="This chat request is still in progress",
            headers={"Retry-After": "2"},
        ) from exc
    except ChatSessionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found") from exc
    return _stream_response(coordinator, accepted)


def _stream_response(
    coordinator: ChatCoordinator,
    accepted: AcceptedChat,
) -> StreamingResponse:
    async def encoded_events() -> AsyncIterator[bytes]:
        event_id = 0
        async for event in coordinator.events(accepted):
            event_id += 1
            yield _encode_event(event, event_id)

    headers = {
        "Cache-Control": "no-cache, no-store",
        "X-Accel-Buffering": "no",
        "X-Idempotent-Replay": "true" if accepted.replay is not None else "false",
    }
    return StreamingResponse(
        encoded_events(),
        media_type="text/event-stream",
        headers=headers,
    )


def _encode_event(event: StreamEvent, event_id: int) -> bytes:
    data = orjson.dumps(event.data)
    return (
        f"id: {event_id}\nevent: {event.event}\ndata: ".encode()
        + data
        + b"\n\n"
    )
