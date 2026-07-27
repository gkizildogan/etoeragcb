from __future__ import annotations

import hashlib
import os
import stat
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import orjson
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, false, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatSession, Feedback, Message, User, UserTenant

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
RUN_SCHEMA_VERSION = 1


class HistoryEvaluationError(RuntimeError):
    pass


class HistoricalTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    example_id: uuid.UUID
    user_message_id: uuid.UUID
    session_id: uuid.UUID
    query: str
    answer: str
    created_at: datetime
    document_ids: tuple[uuid.UUID, ...] = ()
    collection_ids: tuple[uuid.UUID, ...] = ()
    requested_web_search: bool = False
    role_group: Literal["member", "admin", "superuser"]
    role_basis: Literal["current_at_export", "request_time"]
    feedback_rating: Literal[-1, 1] | None = None
    feedback_comment: str | None = None
    original_route: str | None = None
    original_gate: dict[str, Any] | None = None
    recorded_sources: tuple[dict[str, Any], ...] = ()
    recorded_candidates: tuple[dict[str, Any], ...] = ()
    recorded_retrieval_schema_version: int | None = None
    recorded_model_provenance: dict[str, str] = Field(default_factory=dict)


class QueryLabel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    kind: Literal["query"]
    example_id: uuid.UUID
    answerable: bool
    language: Literal["en", "tr", "other"]
    label_source: Literal["human", "llm_suggestion"]
    reviewer: str = Field(min_length=1, max_length=200)
    reviewed_at: datetime
    notes: str | None = None

    @model_validator(mode="after")
    def require_human_confirmation(self) -> QueryLabel:
        if self.label_source != "human":
            raise ValueError("labels must be human-confirmed")
        return self


class CandidateLabel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    kind: Literal["candidate"]
    example_id: uuid.UUID
    candidate_key: str = Field(min_length=1, max_length=1_024)
    relevance: int = Field(ge=0, le=3)
    label_source: Literal["human", "llm_suggestion"]
    reviewer: str = Field(min_length=1, max_length=200)
    reviewed_at: datetime
    notes: str | None = None

    @model_validator(mode="after")
    def require_human_confirmation(self) -> CandidateLabel:
        if self.label_source != "human":
            raise ValueError("labels must be human-confirmed")
        return self


HumanLabel = QueryLabel | CandidateLabel


async def historical_turns(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> tuple[HistoricalTurn, ...]:
    """Select well-formed historical knowledge pairs without exporting identity fields."""

    conditions = [
        Message.tenant_id == tenant_id,
        Message.role == "assistant",
        ChatSession.deleted_at.is_(None),
        User.is_active.is_(True),
        User.disabled_at.is_(None),
    ]
    if start_at is not None:
        conditions.append(Message.created_at >= start_at)
    if end_at is not None:
        conditions.append(Message.created_at < end_at)
    rows = (
        await session.execute(
            select(Message, ChatSession, User, UserTenant, Feedback)
            .join(
                ChatSession,
                and_(
                    ChatSession.id == Message.session_id,
                    ChatSession.tenant_id == Message.tenant_id,
                    ChatSession.user_id == Message.user_id,
                ),
            )
            .join(User, User.id == Message.user_id)
            .join(
                UserTenant,
                and_(
                    UserTenant.user_id == Message.user_id,
                    UserTenant.tenant_id == Message.tenant_id,
                ),
            )
            .outerjoin(
                Feedback,
                and_(
                    Feedback.message_id == Message.id,
                    Feedback.tenant_id == Message.tenant_id,
                    Feedback.user_id == Message.user_id,
                ),
            )
            .where(*conditions)
            .order_by(Message.created_at, Message.id)
        )
    ).all()
    user_ids: set[uuid.UUID] = set()
    parsed_rows: list[
        tuple[Message, User, UserTenant, Feedback | None, uuid.UUID, dict[str, Any]]
    ] = []
    for assistant, _chat, user, membership, feedback in rows:
        metadata = assistant.meta if isinstance(assistant.meta, dict) else {}
        if not _is_knowledge_turn(metadata):
            continue
        user_message_id = _uuid_value(metadata.get("user_message_id"))
        if user_message_id is None:
            continue
        user_ids.add(user_message_id)
        parsed_rows.append((assistant, user, membership, feedback, user_message_id, metadata))
    user_messages = {
        item.id: item
        for item in await session.scalars(
            select(Message).where(
                Message.tenant_id == tenant_id,
                Message.id.in_(user_ids) if user_ids else false(),
                Message.role == "user",
            )
        )
    }
    result: list[HistoricalTurn] = []
    for assistant, user, membership, feedback, user_message_id, metadata in parsed_rows:
        user_message = user_messages.get(user_message_id)
        if (
            user_message is None
            or user_message.session_id != assistant.session_id
            or user_message.user_id != assistant.user_id
            or user_message.created_at > assistant.created_at
        ):
            continue
        request_meta = user_message.meta if isinstance(user_message.meta, dict) else {}
        retrieval = metadata.get("retrieval")
        retrieval_meta = retrieval if isinstance(retrieval, dict) else {}
        gate = retrieval_meta.get("gate")
        gate_meta = gate if isinstance(gate, dict) else None
        sources = retrieval_meta.get("sources")
        recorded_sources = (
            tuple(item for item in sources if isinstance(item, dict))
            if isinstance(sources, list)
            else ()
        )
        pre_rerank = retrieval_meta.get("pre_rerank_candidates")
        recorded_candidates = (
            tuple(item for item in pre_rerank if isinstance(item, dict))
            if isinstance(pre_rerank, list)
            else ()
        )
        request_role = retrieval_meta.get("request_role")
        has_request_role = request_role in {"member", "admin", "superuser"}
        model_provenance = retrieval_meta.get("model_provenance")
        result.append(
            HistoricalTurn(
                example_id=assistant.id,
                user_message_id=user_message.id,
                session_id=assistant.session_id,
                query=user_message.content,
                answer=assistant.content,
                created_at=assistant.created_at,
                document_ids=_uuid_tuple(request_meta.get("document_ids")),
                collection_ids=_uuid_tuple(request_meta.get("collection_ids")),
                requested_web_search=bool(request_meta.get("web_search", False)),
                role_group=(
                    request_role
                    if has_request_role
                    else (
                        "superuser"
                        if user.is_superuser
                        else ("admin" if membership.role == "admin" else "member")
                    )
                ),
                role_basis="request_time" if has_request_role else "current_at_export",
                feedback_rating=feedback.rating if feedback is not None else None,
                feedback_comment=feedback.comment if feedback is not None else None,
                original_route=_string_or_none(metadata.get("route")),
                original_gate=gate_meta,
                recorded_sources=recorded_sources,
                recorded_candidates=recorded_candidates,
                recorded_retrieval_schema_version=_int_or_none(
                    retrieval_meta.get("schema_version")
                ),
                recorded_model_provenance=(
                    {
                        str(key): str(value)
                        for key, value in model_provenance.items()
                        if isinstance(key, str) and isinstance(value, str)
                    }
                    if isinstance(model_provenance, dict)
                    else {}
                ),
            )
        )
    return tuple(result)


def create_private_run_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, mode=PRIVATE_DIRECTORY_MODE, exist_ok=False)
    except FileExistsError as exc:
        raise HistoryEvaluationError("run directory already exists") from exc
    path.chmod(PRIVATE_DIRECTORY_MODE)


def write_private_json(path: Path, value: object, *, overwrite: bool = False) -> None:
    write_private_bytes(
        path,
        orjson.dumps(value, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS) + b"\n",
        overwrite=overwrite,
    )


def write_private_jsonl(
    path: Path,
    records: Iterable[object],
    *,
    overwrite: bool = False,
) -> None:
    payload = b"".join(
        orjson.dumps(record, option=orjson.OPT_SORT_KEYS) + b"\n" for record in records
    )
    write_private_bytes(path, payload, overwrite=overwrite)


def write_private_bytes(path: Path, payload: bytes, *, overwrite: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(PRIVATE_DIRECTORY_MODE)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    try:
        descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
    except FileExistsError as exc:
        raise HistoryEvaluationError(f"{path.name} already exists") from exc
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    path.chmod(PRIVATE_FILE_MODE)


def assert_private_run(path: Path) -> None:
    if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != PRIVATE_DIRECTORY_MODE:
        raise HistoryEvaluationError("run directory must have mode 0700")
    for item in path.iterdir():
        if item.is_file() and stat.S_IMODE(item.stat().st_mode) != PRIVATE_FILE_MODE:
            raise HistoryEvaluationError(f"{item.name} must have mode 0600")


def run_file_hashes(
    run_dir: Path, *, exclude: Sequence[str] = ("manifest.json",)
) -> dict[str, str]:
    return {
        item.name: sha256_file(item)
        for item in sorted(run_dir.iterdir())
        if item.is_file() and item.name not in exclude
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open("rb") as source:
            for line_number, raw in enumerate(source, start=1):
                if not raw.strip():
                    continue
                value = orjson.loads(raw)
                if not isinstance(value, dict):
                    raise HistoryEvaluationError(
                        f"{path.name}:{line_number} must contain a JSON object"
                    )
                records.append(value)
    except (OSError, orjson.JSONDecodeError) as exc:
        raise HistoryEvaluationError(f"cannot read {path.name}: {exc}") from exc
    return records


def now_rfc3339() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _is_knowledge_turn(metadata: dict[str, Any]) -> bool:
    retrieval = metadata.get("retrieval")
    if not isinstance(retrieval, dict):
        return False
    return retrieval.get("intent") == "knowledge" or metadata.get("route") in {
        "answer",
        "no_answer",
        "rag",
    }


def _uuid_tuple(value: object) -> tuple[uuid.UUID, ...]:
    if not isinstance(value, list):
        return ()
    result = [_uuid_value(item) for item in value]
    return tuple(item for item in result if item is not None)


def _uuid_value(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
