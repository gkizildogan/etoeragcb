from __future__ import annotations

import io
import json
import tarfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from prometheus_client import generate_latest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.metrics import Metrics
from app.ingest.indexer import IndexPoint
from app.ingest.storage import LocalDocumentStorage
from app.models import Base, IdempotencyRequest, RefreshToken, Tenant, User, UserTenant
from app.operations.backup import _archive_documents, _extract_documents
from app.operations.maintenance import run_maintenance
from app.operations.status import has_recent_off_machine_backup, read_backup_status
from app.rag.cache import RedisJsonCache, cache_key


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    async def set(self, key: str, value: bytes, *, ex: int) -> None:
        assert ex > 0
        self.values[key] = value

    async def aclose(self) -> None:
        pass


class FakeIndex:
    async def prepare(self) -> None:
        pass

    async def upsert(self, points: list[IndexPoint]) -> None:
        pass

    async def validate_version(
        self,
        *,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
        expected_count: int,
        sample_id: uuid.UUID,
    ) -> None:
        pass

    async def delete_versions(self, *, tenant_id: uuid.UUID, version_ids: list[uuid.UUID]) -> None:
        raise AssertionError("payload GC must be blocked without a verified backup")

    async def close(self) -> None:
        pass


async def test_cache_metrics_use_bounded_labels() -> None:
    metrics = Metrics()
    cache = RedisJsonCache("redis://unused", metrics)
    fake = FakeRedis()
    cache._redis = fake  # type: ignore[assignment]  # isolated Redis adapter fixture
    key = cache_key("retrieval", {"tenant": "not-a-label"})

    assert await cache.get_json(key) is None
    await cache.set_json(key, {"answer": 42}, 60)
    assert await cache.get_json(key) == {"answer": 42}
    await cache.set_json("untrusted:key", {"answer": 0}, 0)

    rendered = generate_latest(metrics.registry).decode()
    assert (
        'rag_cache_operations_total{namespace="retrieval",operation="get",outcome="miss"} 1.0'
        in rendered
    )
    assert (
        'rag_cache_operations_total{namespace="retrieval",operation="get",outcome="hit"} 1.0'
        in rendered
    )
    assert (
        'rag_cache_operations_total{namespace="other",operation="set",outcome="skipped"} 1.0'
        in rendered
    )


def test_backup_status_requires_verified_recent_off_machine_copy(tmp_path: Path) -> None:
    status_file = tmp_path / "last-success.json"
    now = datetime(2026, 7, 23, 12, tzinfo=UTC)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "backup_id": "20260723T110000Z",
        "completed_at": "2026-07-23T11:00:00Z",
        "encrypted": True,
        "repository_checked": True,
        "off_machine_uploaded": True,
        "destination_scheme": "gdrive",
        "restic_snapshot_id": "0123456789abcdef",
    }
    status_file.write_text(json.dumps(payload), encoding="utf-8")

    assert read_backup_status(status_file) is not None
    assert has_recent_off_machine_backup(status_file, max_age_hours=2, now=now)
    payload["off_machine_uploaded"] = False
    status_file.write_text(json.dumps(payload), encoding="utf-8")
    assert not has_recent_off_machine_backup(status_file, max_age_hours=2, now=now)

    metrics = Metrics()
    metrics.refresh_backup_status(status_file, now=now)
    rendered = generate_latest(metrics.registry).decode()
    assert "rag_backup_status 0.0" in rendered
    assert "rag_backup_age_seconds -1.0" in rendered


async def test_maintenance_prunes_ephemeral_rows_but_blocks_payload_gc_without_backup(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    now = datetime(2026, 7, 23, 12, tzinfo=UTC)
    tenant = Tenant(slug="maintenance", name="Maintenance")
    user = User(
        email="maintenance@example.com",
        password_hash="not-used",  # noqa: S106 - inert database fixture
    )
    async with factory() as session:
        session.add_all([tenant, user])
        await session.flush()
        session.add(UserTenant(user_id=user.id, tenant_id=tenant.id, role="admin"))
        session.add(
            RefreshToken(
                user_id=user.id,
                tenant_id=tenant.id,
                token_hash="a" * 64,
                expires_at=now - timedelta(days=10),
            )
        )
        session.add(
            IdempotencyRequest(
                tenant_id=tenant.id,
                user_id=user.id,
                operation="test",
                key="old",
                request_hash="b" * 64,
                status="completed",
                expires_at=now - timedelta(days=10),
            )
        )
        await session.commit()

    try:
        result = await run_maintenance(
            factory,
            FakeIndex(),
            LocalDocumentStorage(tmp_path / "documents"),
            retained_generations=2,
            inactive_version_retention_days=35,
            ephemeral_record_retention_days=7,
            backup_status_file=tmp_path / "missing-status.json",
            gc_backup_max_age_hours=36,
            now=now,
        )
        assert result.ephemeral.refresh_tokens == 1
        assert result.ephemeral.idempotency_requests == 1
        assert not result.garbage_collection_permitted
        async with factory() as session:
            assert list(await session.scalars(select(RefreshToken))) == []
            assert list(await session.scalars(select(IdempotencyRequest))) == []
    finally:
        await engine.dispose()


def test_document_backup_archive_round_trip_and_rejects_links(tmp_path: Path) -> None:
    source = tmp_path / "source"
    stored = source / "tenant" / "document" / "version" / "original.txt"
    stored.parent.mkdir(parents=True)
    stored.write_text("restore me", encoding="utf-8")
    staging = source / ".staging" / "partial"
    staging.parent.mkdir()
    staging.write_text("do not back up", encoding="utf-8")
    archive = tmp_path / "documents.tar"
    destination = tmp_path / "destination"
    destination.mkdir()

    _archive_documents(source, archive)
    _extract_documents(archive, destination)

    restored = destination / stored.relative_to(source)
    assert restored.read_text(encoding="utf-8") == "restore me"
    assert not (destination / ".staging").exists()

    unsafe_archive = tmp_path / "unsafe.tar"
    with tarfile.open(unsafe_archive, "w") as output:
        member = tarfile.TarInfo("documents/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/passwd"
        output.addfile(member, io.BytesIO())
    with pytest.raises(RuntimeError, match="unsafe entry"):
        _extract_documents(unsafe_archive, tmp_path / "unsafe-output")
