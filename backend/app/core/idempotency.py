from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

import orjson
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IdempotencyRequest

IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


class ClaimState(str, Enum):
    CLAIMED = "claimed"
    RECOVERED = "recovered"
    IN_PROGRESS = "in_progress"
    REPLAY = "replay"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class ClaimResult:
    state: ClaimState
    response: dict[str, Any] | None = None
    resource_id: uuid.UUID | None = None


def canonical_request_hash(payload: Any) -> str:
    encoded = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(encoded).hexdigest()


def validate_idempotency_key(key: str) -> str:
    candidate = key.strip()
    if IDEMPOTENCY_KEY_RE.fullmatch(candidate) is None:
        raise ValueError("Idempotency-Key must contain 8-128 safe characters")
    return candidate


async def claim_idempotency(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    operation: str,
    key: str,
    request_hash: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> ClaimResult:
    claimed_at = now or datetime.now(UTC)
    normalized_key = validate_idempotency_key(key)
    values = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "operation": operation,
        "key": normalized_key,
        "request_hash": request_hash,
        "status": "in_progress",
        "expires_at": claimed_at + timedelta(seconds=ttl_seconds),
        "created_at": claimed_at,
        "updated_at": claimed_at,
    }
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        inserted = await session.execute(
            postgresql_insert(IdempotencyRequest)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["tenant_id", "user_id", "operation", "key"])
        )
    elif bind.dialect.name == "sqlite":
        inserted = await session.execute(
            sqlite_insert(IdempotencyRequest)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["tenant_id", "user_id", "operation", "key"])
        )
    else:  # pragma: no cover - supported deployments and tests use PostgreSQL/SQLite
        raise RuntimeError("unsupported idempotency database dialect")
    if inserted.rowcount == 1:
        return ClaimResult(ClaimState.CLAIMED)

    record = await session.scalar(
        select(IdempotencyRequest)
        .where(
            IdempotencyRequest.tenant_id == tenant_id,
            IdempotencyRequest.user_id == user_id,
            IdempotencyRequest.operation == operation,
            IdempotencyRequest.key == normalized_key,
        )
        .with_for_update()
    )
    if record is None:  # pragma: no cover - defensive against external row deletion
        raise RuntimeError("idempotency row disappeared during claim")
    if record.request_hash != request_hash:
        return ClaimResult(ClaimState.CONFLICT)
    if record.status == "completed":
        return ClaimResult(ClaimState.REPLAY, record.response, record.resource_id)
    if record.status == "in_progress" and _as_utc(record.expires_at) > claimed_at:
        return ClaimResult(ClaimState.IN_PROGRESS)

    record.status = "in_progress"
    record.response = None
    record.resource_id = None
    record.expires_at = claimed_at + timedelta(seconds=ttl_seconds)
    record.updated_at = claimed_at
    return ClaimResult(ClaimState.RECOVERED)


async def complete_idempotency(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    operation: str,
    key: str,
    response: dict[str, Any],
    resource_id: uuid.UUID | None,
    now: datetime | None = None,
) -> None:
    record = await _locked_record(session, tenant_id, user_id, operation, key)
    if record.status != "in_progress":
        raise RuntimeError("only an in-progress idempotency claim can be completed")
    record.status = "completed"
    record.response = response
    record.resource_id = resource_id
    record.updated_at = now or datetime.now(UTC)


async def fail_idempotency(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    operation: str,
    key: str,
    now: datetime | None = None,
) -> None:
    record = await _locked_record(session, tenant_id, user_id, operation, key)
    record.status = "failed"
    record.updated_at = now or datetime.now(UTC)


async def _locked_record(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    operation: str,
    key: str,
) -> IdempotencyRequest:
    record = await session.scalar(
        select(IdempotencyRequest)
        .where(
            IdempotencyRequest.tenant_id == tenant_id,
            IdempotencyRequest.user_id == user_id,
            IdempotencyRequest.operation == operation,
            IdempotencyRequest.key == validate_idempotency_key(key),
        )
        .with_for_update()
    )
    if record is None:
        raise RuntimeError("idempotency claim does not exist")
    return record


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
