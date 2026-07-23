from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Any

import orjson
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Feedback, Message


class FeedbackExportError(RuntimeError):
    pass


async def feedback_records(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    include_content: bool,
) -> tuple[dict[str, Any], ...]:
    rows = (
        await session.execute(
            select(Feedback, Message)
            .join(
                Message,
                and_(
                    Message.id == Feedback.message_id,
                    Message.tenant_id == Feedback.tenant_id,
                    Message.user_id == Feedback.user_id,
                ),
            )
            .where(
                Feedback.tenant_id == tenant_id,
                Message.role == "assistant",
            )
            .order_by(Feedback.created_at, Feedback.id)
        )
    ).all()
    user_message_ids = {
        identifier
        for _feedback, assistant in rows
        if (identifier := _user_message_id(assistant.meta)) is not None
    }
    user_messages: dict[uuid.UUID, Message] = {}
    if user_message_ids:
        user_messages = {
            message.id: message
            for message in await session.scalars(
                select(Message).where(
                    Message.tenant_id == tenant_id,
                    Message.id.in_(user_message_ids),
                    Message.role == "user",
                )
            )
        }
    records: list[dict[str, Any]] = []
    for feedback, assistant in rows:
        user_message_id = _user_message_id(assistant.meta)
        records.append(
            _record(
                feedback,
                assistant,
                user_messages.get(user_message_id) if user_message_id is not None else None,
                include_content=include_content,
            )
        )
    return tuple(records)


def write_feedback_jsonl(
    records: tuple[dict[str, Any], ...],
    *,
    path: Path,
    overwrite: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise FeedbackExportError("output exists; pass --overwrite to replace it") from exc
    try:
        with os.fdopen(descriptor, "wb") as output:
            for record in records:
                output.write(orjson.dumps(record, option=orjson.OPT_SORT_KEYS))
                output.write(b"\n")
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    path.chmod(0o600)


def _record(
    feedback: Feedback,
    assistant: Message,
    user_message: Message | None,
    *,
    include_content: bool,
) -> dict[str, Any]:
    metadata = assistant.meta if isinstance(assistant.meta, dict) else {}
    retrieval = metadata.get("retrieval")
    retrieval_meta = retrieval if isinstance(retrieval, dict) else {}
    gate = retrieval_meta.get("gate")
    gate_meta = gate if isinstance(gate, dict) else {}
    citations = metadata.get("citations")
    citation_meta = citations if isinstance(citations, dict) else {}
    query = user_message.content if user_message is not None else ""
    record: dict[str, Any] = {
        "schema_version": 1,
        "feedback_id": str(feedback.id),
        "tenant_id": str(feedback.tenant_id),
        "session_id": str(assistant.session_id),
        "assistant_message_id": str(assistant.id),
        "rating": feedback.rating,
        "comment": feedback.comment,
        "created_at": feedback.created_at.isoformat(),
        "query_sha256": _sha256(query),
        "answer_sha256": _sha256(assistant.content),
        "route": metadata.get("route"),
        "gate_route": gate_meta.get("route"),
        "gate_reasons": gate_meta.get("reasons", []),
        "web_status": retrieval_meta.get("web_status"),
        "citation_markers": sorted(str(marker) for marker in citation_meta),
    }
    if include_content:
        record["query"] = query or None
        record["answer"] = assistant.content
    return record


def _user_message_id(metadata: dict[str, Any]) -> uuid.UUID | None:
    raw = metadata.get("user_message_id")
    if not isinstance(raw, str):
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
