from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from argon2 import PasswordHasher, Type
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.rate_limit import RateLimitDecision
from app.auth.security import SecurityService
from app.config import Settings
from app.ingest.chunker import TokenSpan, chunk_blocks
from app.ingest.embedder import TeiClient
from app.ingest.indexer import IndexPoint, IndexValidationError
from app.ingest.jobs import IngestionPipeline
from app.ingest.normalization import normalize_lexical
from app.ingest.parsers import parse_document
from app.ingest.reconcile import (
    active_manifest_version_ids,
    garbage_collect_inactive_versions,
    reconcile_ingestion_jobs,
    tombstone_document,
)
from app.ingest.sections import build_sections
from app.ingest.storage import LocalDocumentStorage
from app.main import create_app
from app.models import (
    Base,
    Chunk,
    Document,
    DocumentVersion,
    IngestionJob,
    Tenant,
    User,
    UserTenant,
)


class FakeChecker:
    async def check(self) -> dict[str, bool]:
        return {"postgres": True}

    async def close(self) -> None:
        pass


class AllowRateLimiter:
    async def check(
        self, scope: str, identifiers: dict[str, str], limits: list[str]
    ) -> RateLimitDecision:
        return RateLimitDecision(True)

    async def register_failure(self, subject: str) -> None:
        pass

    async def clear_failures(self, subject: str) -> None:
        pass

    async def close(self) -> None:
        pass


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: list[uuid.UUID] = []

    async def enqueue(self, job_id: uuid.UUID) -> str:
        self.jobs.append(job_id)
        return f"ingestion:{job_id}"


class WhitespaceTokenizer:
    async def token_spans(self, text: str) -> list[TokenSpan]:
        return [TokenSpan(match.start(), match.end()) for match in re.finditer(r"\S+", text)]


class FakeEmbedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text)), 1.0, 0.0, 0.5] for text in texts]


class FakeIndex:
    def __init__(self) -> None:
        self.points: dict[uuid.UUID, IndexPoint] = {}

    async def prepare(self) -> None:
        pass

    async def upsert(self, points: list[IndexPoint]) -> None:
        self.points.update({point.id: point for point in points})

    async def validate_version(
        self,
        *,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
        expected_count: int,
        sample_id: uuid.UUID,
    ) -> None:
        matching = [
            point
            for point in self.points.values()
            if point.payload["tenant_id"] == str(tenant_id)
            and point.payload["document_version_id"] == str(version_id)
        ]
        if len(matching) != expected_count or sample_id not in self.points:
            raise IndexValidationError("fake staged validation failed")

    async def delete_versions(self, *, tenant_id: uuid.UUID, version_ids: list[uuid.UUID]) -> None:
        version_values = {str(item) for item in version_ids}
        self.points = {
            point_id: point
            for point_id, point in self.points.items()
            if not (
                point.payload["tenant_id"] == str(tenant_id)
                and point.payload["document_version_id"] in version_values
            )
        }

    async def close(self) -> None:
        pass


async def test_jsonl_hierarchy_original_lexical_and_occurrence_ids(tmp_path: Path) -> None:
    path = tmp_path / "repeated.jsonl"
    rows = [
        {
            "text": "I İ \u0131 i — Ayn\u0131 passage",
            "source_page": "First",
            "category": "Türkçe",
            "word_count": 7,
        },
        {
            "text": "I İ \u0131 i — Ayn\u0131 passage",
            "source_page": "Second",
            "category": "Türkçe",
            "word_count": 7,
        },
    ]
    path.write_text("".join(f"{json.dumps(row, ensure_ascii=False)}\n" for row in rows))
    blocks = parse_document(path, "application/x-ndjson", expanded_limit_bytes=1_000_000)
    version_id = uuid.uuid4()
    sections, sectioned = build_sections(version_id, blocks)
    first = await chunk_blocks(
        version_id,
        sectioned,
        WhitespaceTokenizer(),
        max_tokens=64,
        overlap=8,
    )
    second = await chunk_blocks(
        version_id,
        sectioned,
        WhitespaceTokenizer(),
        max_tokens=64,
        overlap=8,
    )

    assert [chunk.id for chunk in first] == [chunk.id for chunk in second]
    assert first[0].id != first[1].id
    assert first[0].content_sha256 == first[1].content_sha256
    assert [chunk.occurrence_index for chunk in first] == [0, 1]
    assert first[0].text_original == rows[0]["text"]
    assert normalize_lexical("I İ \u0131 i", language="tr") == "\u0131 i \u0131 i"
    assert [(section.level, section.heading_original) for section in sections] == [
        (1, "Türkçe"),
        (2, "First"),
        (2, "Second"),
    ]


async def test_tei_offsets_are_utf8_safe_and_oversized_text_is_split() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        text = body["inputs"]
        if text.startswith("one") and len(text) > 7:
            return httpx.Response(422, json={"error": "token limit"}, request=request)
        tokens = []
        for match in re.finditer(r"\s*\S+", text):
            tokens.append(
                {
                    "id": len(tokens),
                    "text": match.group(),
                    "special": False,
                    "start": len(text[: match.start()].encode()),
                    "stop": len(text[: match.end()].encode()),
                }
            )
        return httpx.Response(200, json=tokens, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://tei"
    ) as client:
        tokenizer = TeiClient("http://tei", expected_dimension=4, client=client)
        turkish = "İstanbul dünya"
        turkish_spans = await tokenizer.token_spans(turkish)
        split_spans = await tokenizer.token_spans("one two three")
    assert turkish_spans == [TokenSpan(0, 8), TokenSpan(8, 14)]
    assert turkish[turkish_spans[1].start : turkish_spans[1].end] == " dünya"
    assert split_spans == [TokenSpan(0, 3), TokenSpan(4, 7), TokenSpan(8, 13)]


def test_prepared_local_fixture_is_parseable_when_present() -> None:
    path = Path(__file__).parents[2] / "docstoingest" / "test.jsonl"
    if not path.exists():
        return
    blocks = parse_document(path, "application/x-ndjson", expanded_limit_bytes=5_000_000)
    assert len(blocks) == 314
    assert all(block.text_original and len(block.heading_path) == 2 for block in blocks)
    assert {"source_page", "category", "word_count", "jsonl_line"} <= set(blocks[0].source_metadata)


async def test_upload_is_validated_idempotent_and_admin_only(settings: Settings) -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    security = SecurityService(
        settings,
        password_hasher=PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, type=Type.ID),
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        tenant = Tenant(slug="upload", name="Upload")
        stored_hash = security.hash_password("Upload test credential 1!")
        admin = User(email="upload-admin@example.com", password_hash=stored_hash)
        member = User(email="upload-member@example.com", password_hash=stored_hash)
        session.add_all([tenant, admin, member])
        await session.flush()
        session.add_all(
            [
                UserTenant(user_id=admin.id, tenant_id=tenant.id, role="admin"),
                UserTenant(user_id=member.id, tenant_id=tenant.id, role="member"),
            ]
        )
        await session.commit()
    admin_token = security.issue_access_token(
        user_id=admin.id,
        tenant_id=tenant.id,
        role="admin",
        auth_version=admin.auth_version,
    )[0]
    member_token = security.issue_access_token(
        user_id=member.id,
        tenant_id=tenant.id,
        role="member",
        auth_version=member.auth_version,
    )[0]
    queue = FakeQueue()
    app = create_app(
        settings,
        FakeChecker(),
        session_factory=factory,
        rate_limiter=AllowRateLimiter(),
        security=security,
        ingestion_queue=queue,
    )
    row = _row("Fixture", "A small upload for idempotency.")
    content = f"{json.dumps(row)}\n".encode()
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://rag.example.com",
            ) as client:
                headers = {
                    "Authorization": f"Bearer {admin_token}",
                    "Idempotency-Key": "upload-fixture-0001",
                }
                first = await client.post(
                    "/api/documents",
                    headers=headers,
                    data={"title": "Fixture"},
                    files={"file": ("fixture.jsonl", content, "application/x-ndjson")},
                )
                replay = await client.post(
                    "/api/documents",
                    headers=headers,
                    data={"title": "Fixture"},
                    files={"file": ("fixture.jsonl", content, "application/x-ndjson")},
                )
                conflict = await client.post(
                    "/api/documents",
                    headers=headers,
                    data={"title": "Changed"},
                    files={"file": ("fixture.jsonl", content, "application/x-ndjson")},
                )
                forbidden = await client.post(
                    "/api/documents",
                    headers={
                        "Authorization": f"Bearer {member_token}",
                        "Idempotency-Key": "upload-fixture-0002",
                    },
                    data={"title": "Forbidden"},
                    files={"file": ("fixture.jsonl", content, "application/x-ndjson")},
                )
                invalid = await client.post(
                    "/api/documents",
                    headers={
                        "Authorization": f"Bearer {admin_token}",
                        "Idempotency-Key": "upload-fixture-0003",
                    },
                    data={"title": "Invalid"},
                    files={"file": ("fixture.exe", content, "application/octet-stream")},
                )
        assert first.status_code == replay.status_code == 202
        assert first.json() == replay.json()
        assert conflict.status_code == 409
        assert forbidden.status_code == 403
        assert invalid.status_code == 422
        assert len(queue.jobs) == 1
        async with factory() as session:
            assert len(list(await session.scalars(select(Document)))) == 1
            assert len(list(await session.scalars(select(DocumentVersion)))) == 1
            jobs = list(await session.scalars(select(IngestionJob)))
            assert len(jobs) == 1 and jobs[0].status == "queued"
    finally:
        await engine.dispose()


async def test_activation_failure_reconciliation_and_tombstone(tmp_path: Path) -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    storage = LocalDocumentStorage(tmp_path / "documents")
    fake_index = FakeIndex()
    try:
        tenant_id, document_id, first_version, first_job = await _seed_document_version(
            factory,
            storage,
            rows=[_row("First", "The stable answer is red.")],
            version_number=1,
        )
        pipeline = _pipeline(factory, storage, fake_index)
        await pipeline.process(first_job)
        async with factory() as session:
            document = await session.get(Document, document_id)
            tenant = await session.get(Tenant, tenant_id)
            assert document is not None and document.active_version_id == first_version
            assert tenant is not None and tenant.active_index_generation_id is not None
            first_generation = tenant.active_index_generation_id
            assert tenant.retrieval_revision == 2
            assert await active_manifest_version_ids(session, tenant_id=tenant_id) == [
                first_version
            ]

        second_version, second_job = await _add_version(
            factory,
            storage,
            tenant_id=tenant_id,
            document_id=document_id,
            version_number=2,
            rows=[_row("Second", "The changed answer is blue.")],
        )

        def fail_before_activation(stage: str) -> None:
            if stage == "before_activation":
                raise RuntimeError("injected activation failure")

        failing = _pipeline(factory, storage, fake_index, failure_injector=fail_before_activation)
        with pytest.raises(RuntimeError, match="injected activation failure"):
            await failing.process(second_job)
        async with factory() as session:
            document = await session.get(Document, document_id)
            tenant = await session.get(Tenant, tenant_id)
            failed = await session.get(DocumentVersion, second_version)
            assert document is not None and document.active_version_id == first_version
            assert tenant is not None and tenant.active_index_generation_id == first_generation
            assert failed is not None and failed.status == "failed"
            assert await active_manifest_version_ids(session, tenant_id=tenant_id) == [
                first_version
            ]

        third_version, third_job = await _add_version(
            factory,
            storage,
            tenant_id=tenant_id,
            document_id=document_id,
            version_number=3,
            rows=[_row("Third", "The recovered answer is green.")],
            job_status="processing",
            heartbeat=datetime.now(UTC),
        )
        await pipeline.process(third_job)
        async with factory() as session:
            document = await session.get(Document, document_id)
            fresh_job = await session.get(IngestionJob, third_job)
            assert document is not None and document.active_version_id == first_version
            assert fresh_job is not None and fresh_job.attempt == 0
            fresh_job.heartbeat_at = datetime.now(UTC) - timedelta(hours=1)
            await session.commit()
        reconciled = await reconcile_ingestion_jobs(
            factory,
            pipeline,
            stale_after_seconds=300,
        )
        assert reconciled == [third_job]
        async with factory() as session:
            document = await session.get(Document, document_id)
            active = await session.get(DocumentVersion, third_version)
            old = await session.get(DocumentVersion, first_version)
            assert document is not None and document.active_version_id == third_version
            assert active is not None and active.status == "active"
            assert old is not None and old.status == "superseded"
            assert await active_manifest_version_ids(session, tenant_id=tenant_id) == [
                third_version
            ]
            revision = await tombstone_document(
                session, tenant_id=tenant_id, document_id=document_id
            )
            assert revision == 4
        async with factory() as session:
            assert await active_manifest_version_ids(session, tenant_id=tenant_id) == []
            chunks = list(
                await session.scalars(
                    select(Chunk).where(Chunk.document_version_id == third_version)
                )
            )
            assert chunks
        result = await garbage_collect_inactive_versions(
            factory,
            fake_index,
            storage,
            tenant_id=tenant_id,
            retained_generations=2,
        )
        assert result.versions == 2
        async with factory() as session:
            retained = await session.get(DocumentVersion, third_version)
            assert retained is not None and storage.resolve(retained.storage_key).exists()
            assert list(
                await session.scalars(
                    select(Chunk).where(Chunk.document_version_id == third_version)
                )
            )
        indexed_versions = {
            point.payload["document_version_id"] for point in fake_index.points.values()
        }
        assert str(first_version) not in indexed_versions
        assert str(second_version) not in indexed_versions
        assert str(third_version) in indexed_versions
        async with factory() as session:
            tenant = await session.get(Tenant, tenant_id)
            assert tenant is not None
            deleted_generation = tenant.active_index_generation_id
        await pipeline.process(first_job)
        async with factory() as session:
            tenant = await session.get(Tenant, tenant_id)
            assert tenant is not None
            assert tenant.active_index_generation_id == deleted_generation
            assert await active_manifest_version_ids(session, tenant_id=tenant_id) == []
    finally:
        await engine.dispose()


def _pipeline(
    factory: async_sessionmaker[AsyncSession],
    storage: LocalDocumentStorage,
    fake_index: FakeIndex,
    *,
    failure_injector: Callable[[str], None] | None = None,
) -> IngestionPipeline:
    return IngestionPipeline(
        factory,
        storage,
        WhitespaceTokenizer(),
        FakeEmbedder(),
        fake_index,
        chunk_tokens=64,
        chunk_overlap=8,
        batch_size=2,
        upload_max_bytes=5_000_000,
        heartbeat_timeout=300,
        failure_injector=failure_injector,
    )


async def _seed_document_version(
    factory: async_sessionmaker[AsyncSession],
    storage: LocalDocumentStorage,
    *,
    rows: list[dict[str, object]],
    version_number: int,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant = Tenant(slug="p4", name="P4")
    user = User(email="p4@example.com", password_hash=f"fixture-{uuid.uuid4()}")
    async with factory() as session:
        session.add_all([tenant, user])
        await session.flush()
        session.add(UserTenant(user_id=user.id, tenant_id=tenant.id, role="admin"))
        document = Document(
            tenant_id=tenant.id,
            title="Fixture",
            source_filename="fixture.jsonl",
            mime="application/x-ndjson",
            created_by=user.id,
        )
        session.add(document)
        await session.flush()
        version_id, job_id = await _insert_version(
            session,
            storage,
            tenant_id=tenant.id,
            document_id=document.id,
            version_number=version_number,
            rows=rows,
        )
        await session.commit()
        return tenant.id, document.id, version_id, job_id


async def _add_version(
    factory: async_sessionmaker[AsyncSession],
    storage: LocalDocumentStorage,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    version_number: int,
    rows: list[dict[str, object]],
    job_status: str = "staged",
    heartbeat: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with factory() as session:
        result = await _insert_version(
            session,
            storage,
            tenant_id=tenant_id,
            document_id=document_id,
            version_number=version_number,
            rows=rows,
            job_status=job_status,
            heartbeat=heartbeat,
        )
        await session.commit()
        return result


async def _insert_version(
    session: AsyncSession,
    storage: LocalDocumentStorage,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    version_number: int,
    rows: list[dict[str, object]],
    job_status: str = "staged",
    heartbeat: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    version_id = uuid.uuid4()
    relative = Path(str(tenant_id)) / str(document_id) / str(version_id) / "original.jsonl"
    path = storage.root / relative
    path.parent.mkdir(parents=True, mode=0o700)
    content = "".join(f"{json.dumps(row)}\n" for row in rows)
    path.write_text(content)
    version = DocumentVersion(
        id=version_id,
        tenant_id=tenant_id,
        document_id=document_id,
        version=version_number,
        file_sha256=f"{version_number:064x}",
        file_size_bytes=len(content.encode()),
        storage_key=relative.as_posix(),
        status="processing" if job_status == "processing" else "staged",
    )
    job = IngestionJob(
        tenant_id=tenant_id,
        document_version_id=version_id,
        status=job_status,
        heartbeat_at=heartbeat,
    )
    session.add_all([version, job])
    await session.flush()
    return version.id, job.id


def _row(page: str, text: str) -> dict[str, object]:
    return {
        "text": text,
        "source_page": page,
        "category": "Facts",
        "word_count": len(text.split()),
    }
