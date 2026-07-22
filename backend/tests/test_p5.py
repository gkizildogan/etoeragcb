from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.ingest.hashing import sparse_lexical_vector
from app.ingest.normalization import normalize_lexical
from app.models import (
    Base,
    Chunk,
    Document,
    DocumentCollection,
    DocumentVersion,
    IndexGeneration,
    IndexGenerationDocument,
    KnowledgeCollection,
    Section,
    Tenant,
    User,
    UserTenant,
)
from app.rag.cache import JsonCache
from app.rag.planner import PlanningResult, RetrievalPlan, VllmPlanner
from app.rag.retriever import (
    BranchHit,
    BranchResults,
    HybridRetriever,
    QdrantHybridSearch,
)
from app.rag.scope import MetadataResolver, ScopeValidationError
from app.rag.service import RetrievalService


class FakePlanner:
    def __init__(self, result: PlanningResult) -> None:
        self.result = result
        self.calls = 0

    async def plan(self, message: str) -> PlanningResult:
        self.calls += 1
        return self.result


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[1.0, 0.0, 0.0, float(len(text))] for text in texts]


class FakeHybridBackend:
    def __init__(self, dense: list[uuid.UUID], sparse: list[uuid.UUID]) -> None:
        self.dense = dense
        self.sparse = sparse
        self.calls = 0
        self.scopes = []

    async def query_branches(self, **kwargs: Any) -> BranchResults:
        self.calls += 1
        self.scopes.append(kwargs["scope"])
        return BranchResults(
            dense=tuple(
                BranchHit(chunk_id=chunk_id, score=1.0 - rank / 100)
                for rank, chunk_id in enumerate(self.dense)
            ),
            sparse=tuple(
                BranchHit(chunk_id=chunk_id, score=2.0 - rank / 100)
                for rank, chunk_id in enumerate(self.sparse)
            ),
        )

    async def close(self) -> None:
        pass


class MemoryCache(JsonCache):
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    async def get_json(self, key: str) -> dict[str, Any] | None:
        return self.values.get(key)

    async def set_json(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        self.values[key] = value


@dataclass(frozen=True, slots=True)
class Corpus:
    tenant_id: uuid.UUID
    other_tenant_id: uuid.UUID
    collection_id: uuid.UUID
    document_id: uuid.UUID
    ambiguous_document_ids: tuple[uuid.UUID, uuid.UUID]
    other_document_id: uuid.UUID
    version_id: uuid.UUID
    network_section_id: uuid.UUID
    chunk_ids: tuple[uuid.UUID, ...]
    repeated_chunk_ids: tuple[uuid.UUID, uuid.UUID]
    other_chunk_id: uuid.UUID


async def test_vllm_planner_enforces_schema_and_falls_back_safely() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            content = json.dumps(
                {
                    "intent": "knowledge",
                    "query": "ZX-42 ağ ayar\u0131",
                    "exact_terms": ["ZX-42"],
                    "document_hints": [],
                    "collection_hints": ["Teknik"],
                    "heading_hints": ["Ağ"],
                }
            )
        else:
            content = "not-json"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://vllm"
    ) as client:
        planner = VllmPlanner("http://vllm", "fixture-model", client=client)
        valid = await planner.plan("Teknik koleksiyonunda ZX-42 ağ ayar\u0131")
        fallback = await planner.plan('Find "ZX-42" and ABC123 in the runbook')

    assert not valid.used_fallback
    assert valid.plan.collection_hints == ("Teknik",)
    assert fallback.used_fallback and fallback.plan.intent == "knowledge"
    assert fallback.plan.exact_terms == ("ZX-42", "ABC123")
    assert requests[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert requests[0]["response_format"]["json_schema"]["strict"] is True
    assert requests[0]["response_format"]["json_schema"]["schema"]["additionalProperties"] is False


async def test_metadata_resolution_is_authorized_explainable_and_bounded(tmp_path: Path) -> None:
    engine, factory, corpus = await _seed_corpus(tmp_path)
    resolver = MetadataResolver()
    try:
        plan = RetrievalPlan(
            intent="knowledge",
            query="Ağ kurulumu",
            exact_terms=[],
            document_hints=["Runbook"],
            collection_hints=["Teknik"],
            heading_hints=["Kurulum"],
        )
        async with factory() as session:
            scope = await resolver.resolve(session, tenant_id=corpus.tenant_id, plan=plan)
            with pytest.raises(ScopeValidationError):
                await resolver.resolve(
                    session,
                    tenant_id=corpus.tenant_id,
                    plan=plan,
                    explicit_document_ids=(corpus.other_document_id,),
                )
        assert scope.document_ids == (corpus.document_id,)
        assert scope.version_ids == (corpus.version_id,)
        assert scope.section_ids is not None
        assert corpus.network_section_id in scope.section_ids
        assert any(
            decision.kind == "heading"
            and decision.resolution == "exact_scope"
            and decision.expanded_section_count == 2
            for decision in scope.decisions
        )

        ambiguous = RetrievalPlan(
            intent="knowledge",
            query="shared policy",
            exact_terms=[],
            document_hints=["Shared"],
            collection_hints=[],
            heading_hints=["Identifiers"],
        )
        async with factory() as session:
            ambiguous_scope = await resolver.resolve(
                session, tenant_id=corpus.tenant_id, plan=ambiguous
            )
        assert set(corpus.ambiguous_document_ids) <= set(ambiguous_scope.document_ids)
        assert any(
            decision.kind == "document" and decision.resolution == "ambiguous_boost"
            for decision in ambiguous_scope.decisions
        )
        assert any(boost.hint == "Shared" for boost in ambiguous_scope.boosts)
    finally:
        await engine.dispose()


async def test_hybrid_retrieval_scopes_both_branches_and_limits_neighbors(
    tmp_path: Path,
) -> None:
    engine, factory, corpus = await _seed_corpus(tmp_path)
    resolver = MetadataResolver()
    backend = FakeHybridBackend(
        dense=[corpus.chunk_ids[3], corpus.other_chunk_id, corpus.chunk_ids[0]],
        sparse=[corpus.chunk_ids[0], corpus.chunk_ids[3]],
    )
    retriever = HybridRetriever(
        backend,
        dense_limit=10,
        sparse_limit=10,
        pool_limit=6,
        section_chunk_limit=4,
        neighbor_radius=1,
    )
    plan = RetrievalPlan(
        intent="knowledge",
        query="ZX-42 network setting",
        exact_terms=["ZX-42"],
        document_hints=["Runbook"],
        collection_hints=["Teknik"],
        heading_hints=["Ağ"],
    )
    try:
        async with factory() as session:
            scope = await resolver.resolve(session, tenant_id=corpus.tenant_id, plan=plan)
            candidates = await retriever.retrieve(
                session,
                dense=[1.0, 0.0, 0.0, 0.0],
                sparse=sparse_lexical_vector(normalize_lexical(plan.query)),
                exact_terms=tuple(plan.exact_terms),
                scope=scope,
            )
        assert backend.calls == 1
        assert backend.scopes[0].version_ids == (corpus.version_id,)
        assert all(candidate.tenant_id == corpus.tenant_id for candidate in candidates)
        assert corpus.other_chunk_id not in {candidate.chunk_id for candidate in candidates}
        assert candidates[0].chunk_id == corpus.chunk_ids[0]
        assert candidates[0].exact_rank == 1
        semantic = next(item for item in candidates if item.chunk_id == corpus.chunk_ids[3])
        assert semantic.dense_rank == 1
        assert len(candidates) <= 4
        assert any(candidate.is_neighbor for candidate in candidates)
        assert all(candidate.section_id == corpus.network_section_id for candidate in candidates)
    finally:
        await engine.dispose()


async def test_repeated_pages_remain_distinct_and_no_answer_is_empty(tmp_path: Path) -> None:
    engine, factory, corpus = await _seed_corpus(tmp_path)
    resolver = MetadataResolver()
    plan = RetrievalPlan(
        intent="knowledge",
        query="repeated bilingual passage",
        exact_terms=[],
        document_hints=["Shared"],
        collection_hints=[],
        heading_hints=["Identifiers"],
    )
    backend = FakeHybridBackend(
        dense=list(corpus.repeated_chunk_ids),
        sparse=list(reversed(corpus.repeated_chunk_ids)),
    )
    retriever = HybridRetriever(
        backend,
        dense_limit=10,
        sparse_limit=10,
        pool_limit=8,
        section_chunk_limit=4,
        neighbor_radius=0,
    )
    try:
        async with factory() as session:
            scope = await resolver.resolve(session, tenant_id=corpus.tenant_id, plan=plan)
            candidates = await retriever.retrieve(
                session,
                dense=[1.0, 0.0, 0.0, 0.0],
                sparse=sparse_lexical_vector(normalize_lexical(plan.query)),
                exact_terms=(),
                scope=scope,
            )
            empty_backend = FakeHybridBackend([], [])
            empty = await HybridRetriever(
                empty_backend,
                dense_limit=10,
                sparse_limit=10,
                pool_limit=8,
                section_chunk_limit=4,
                neighbor_radius=1,
            ).retrieve(
                session,
                dense=[1.0, 0.0, 0.0, 0.0],
                sparse=sparse_lexical_vector("missing"),
                exact_terms=(),
                scope=scope,
            )
        repeated = [item for item in candidates if item.chunk_id in corpus.repeated_chunk_ids]
        assert len(repeated) == 2
        assert {item.page_start for item in repeated} == {1, 2}
        assert repeated[0].text_original == repeated[1].text_original
        assert empty == ()
    finally:
        await engine.dispose()


async def test_retrieval_cache_includes_revision_and_planner_failure_remains_hybrid(
    tmp_path: Path,
) -> None:
    engine, factory, corpus = await _seed_corpus(tmp_path)
    planning = PlanningResult(
        plan=RetrievalPlan(
            intent="knowledge",
            query="ZX-42 ağ ayar\u0131",
            exact_terms=["ZX-42"],
            document_hints=["Runbook"],
            collection_hints=[],
            heading_hints=[],
        ),
        used_fallback=True,
        fallback_reason="invalid_response",
    )
    planner = FakePlanner(planning)
    embedder = FakeEmbedder()
    backend = FakeHybridBackend([corpus.chunk_ids[0]], [corpus.chunk_ids[0]])
    service = RetrievalService(
        planner,
        MetadataResolver(),
        embedder,
        HybridRetriever(
            backend,
            dense_limit=10,
            sparse_limit=10,
            pool_limit=8,
            section_chunk_limit=4,
            neighbor_radius=0,
        ),
        cache=MemoryCache(),
        plan_cache_ttl=300,
        retrieval_cache_ttl=300,
        planner_revision="planner-fixture",
        embedding_revision="embed-fixture",
    )
    try:
        async with factory() as session:
            first = await service.retrieve(
                session, tenant_id=corpus.tenant_id, message="broken planner request"
            )
            second = await service.retrieve(
                session, tenant_id=corpus.tenant_id, message="broken planner request"
            )
        assert first.planning.used_fallback and first.candidates
        assert not first.cache_hit and second.cache_hit
        assert backend.calls == embedder.calls == 1
        assert planner.calls == 1

        async with factory() as session:
            tenant = await session.get(Tenant, corpus.tenant_id)
            assert tenant is not None
            tenant.retrieval_revision += 1
            await session.commit()
        async with factory() as session:
            revised = await service.retrieve(
                session, tenant_id=corpus.tenant_id, message="broken planner request"
            )
        assert not revised.cache_hit
        assert backend.calls == embedder.calls == 2
    finally:
        await engine.dispose()


async def test_qdrant_adapter_uses_an_identical_filter_for_dense_and_sparse() -> None:
    class Response:
        points: ClassVar[list[Any]] = []

    class Client:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def query_points(self, **kwargs: Any) -> Response:
            self.calls.append(kwargs)
            return Response()

        async def close(self) -> None:
            pass

    client = Client()
    tenant_id = uuid.uuid4()
    document_id = uuid.uuid4()
    version_id = uuid.uuid4()
    section_id = uuid.uuid4()
    from app.rag.scope import ResolvedScope

    scope = ResolvedScope(
        tenant_id=tenant_id,
        generation_id=1,
        retrieval_revision=2,
        document_ids=(document_id,),
        version_ids=(version_id,),
        section_ids=(section_id,),
    )
    adapter = QdrantHybridSearch("http://qdrant", "chunks", client=client)  # type: ignore[arg-type]
    await adapter.query_branches(
        dense=[1.0, 0.0],
        sparse=sparse_lexical_vector("zx 42"),
        scope=scope,
        dense_limit=10,
        sparse_limit=10,
    )
    assert len(client.calls) == 2
    assert client.calls[0]["query_filter"] is client.calls[1]["query_filter"]
    serialized = client.calls[0]["query_filter"].model_dump(mode="json")
    assert str(tenant_id) in str(serialized)
    assert str(version_id) in str(serialized)
    assert str(document_id) in str(serialized)
    assert str(section_id) in str(serialized)


async def _seed_corpus(
    tmp_path: Path,
) -> tuple[Any, async_sessionmaker[AsyncSession], Corpus]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        tenant = Tenant(slug=f"p5-{uuid.uuid4()}", name="P5")
        other_tenant = Tenant(slug=f"other-{uuid.uuid4()}", name="Other")
        user = User(
            email=f"p5-{uuid.uuid4()}@example.com",
            password_hash=f"fixture-{uuid.uuid4()}",
        )
        other_user = User(
            email=f"other-{uuid.uuid4()}@example.com",
            password_hash=f"fixture-{uuid.uuid4()}",
        )
        session.add_all([tenant, other_tenant, user, other_user])
        await session.flush()
        session.add_all(
            [
                UserTenant(user_id=user.id, tenant_id=tenant.id, role="admin"),
                UserTenant(user_id=other_user.id, tenant_id=other_tenant.id, role="admin"),
            ]
        )
        await session.flush()
        collection = KnowledgeCollection(
            tenant_id=tenant.id,
            name="Teknik",
            description="Turkish technical documents",
            created_by=user.id,
        )
        session.add(collection)
        documents = [
            Document(
                tenant_id=tenant.id,
                title="Runbook",
                source_filename="runbook.md",
                mime="text/markdown",
                created_by=user.id,
            ),
            Document(
                tenant_id=tenant.id,
                title="Shared",
                source_filename="shared-a.txt",
                mime="text/plain",
                created_by=user.id,
            ),
            Document(
                tenant_id=tenant.id,
                title="Shared",
                source_filename="shared-b.txt",
                mime="text/plain",
                created_by=user.id,
            ),
        ]
        other_document = Document(
            tenant_id=other_tenant.id,
            title="Secret",
            source_filename="secret.txt",
            mime="text/plain",
            created_by=other_user.id,
        )
        session.add_all([*documents, other_document])
        await session.flush()
        versions = [
            DocumentVersion(
                tenant_id=tenant.id,
                document_id=document.id,
                version=1,
                file_sha256=f"{index + 1:064x}",
                file_size_bytes=100,
                storage_key=f"p5/{document.id}/v1",
                status="active",
                page_count=2,
                section_count=2,
                chunk_count=6,
            )
            for index, document in enumerate(documents)
        ]
        other_version = DocumentVersion(
            tenant_id=other_tenant.id,
            document_id=other_document.id,
            version=1,
            file_sha256=f"{99:064x}",
            file_size_bytes=100,
            storage_key=f"p5/{other_document.id}/v1",
            status="active",
            page_count=1,
            section_count=1,
            chunk_count=1,
        )
        session.add_all([*versions, other_version])
        await session.flush()
        session.add(
            DocumentCollection(
                tenant_id=tenant.id,
                document_id=documents[0].id,
                collection_id=collection.id,
            )
        )

        runbook_root = _section(
            tenant.id, documents[0].id, versions[0].id, 0, 1, "Kurulum", "Kurulum"
        )
        network = _section(
            tenant.id,
            documents[0].id,
            versions[0].id,
            1,
            2,
            "Ağ",
            "Kurulum / Ağ",
            parent_id=runbook_root.id,
        )
        shared_sections = [
            _section(
                tenant.id,
                document.id,
                version.id,
                0,
                1,
                "Identifiers",
                "Identifiers",
            )
            for document, version in zip(documents[1:], versions[1:], strict=True)
        ]
        secret_section = _section(
            other_tenant.id,
            other_document.id,
            other_version.id,
            0,
            1,
            "Secret",
            "Secret",
        )
        session.add_all([runbook_root, network, *shared_sections, secret_section])
        await session.flush()

        runbook_texts = [
            "ZX-42 ağ ayar\u0131 yaln\u0131zca güvenli VLAN üzerinde etkinleştirilir.",
            "A neighboring Turkish instruction for the network adapter.",
            "Routine network verification step.",
            "Semantic answer: rotate the service credential before deployment.",
            "Additional bounded network evidence.",
            "Last network paragraph that must not import the whole chapter.",
        ]
        runbook_chunks = [
            _chunk(
                tenant.id,
                documents[0].id,
                versions[0].id,
                network.id,
                index,
                index + 1,
                text,
            )
            for index, text in enumerate(runbook_texts)
        ]
        repeated_text = "Repeated bilingual passage / Tekrarlanan iki dilli bölüm."
        repeated_chunks = [
            _chunk(
                tenant.id,
                document.id,
                version.id,
                section.id,
                0,
                page,
                repeated_text,
            )
            for page, (document, version, section) in enumerate(
                zip(documents[1:], versions[1:], shared_sections, strict=True), start=1
            )
        ]
        secret_chunk = _chunk(
            other_tenant.id,
            other_document.id,
            other_version.id,
            secret_section.id,
            0,
            1,
            "ZX-42 cross tenant secret",
        )
        session.add_all([*runbook_chunks, *repeated_chunks, secret_chunk])
        await session.flush()

        generation = IndexGeneration(
            tenant_id=tenant.id,
            reason="fixture",
            status="active",
            retrieval_revision=2,
        )
        other_generation = IndexGeneration(
            tenant_id=other_tenant.id,
            reason="fixture",
            status="active",
            retrieval_revision=2,
        )
        session.add_all([generation, other_generation])
        await session.flush()
        for document, version in zip(documents, versions, strict=True):
            session.add(
                IndexGenerationDocument(
                    generation_id=generation.id,
                    tenant_id=tenant.id,
                    document_id=document.id,
                    document_version_id=version.id,
                )
            )
            document.active_version_id = version.id
            version.index_generation_id = generation.id
        session.add(
            IndexGenerationDocument(
                generation_id=other_generation.id,
                tenant_id=other_tenant.id,
                document_id=other_document.id,
                document_version_id=other_version.id,
            )
        )
        other_document.active_version_id = other_version.id
        other_version.index_generation_id = other_generation.id
        tenant.active_index_generation_id = generation.id
        tenant.retrieval_revision = 2
        other_tenant.active_index_generation_id = other_generation.id
        other_tenant.retrieval_revision = 2
        await session.commit()
        return (
            engine,
            factory,
            Corpus(
                tenant_id=tenant.id,
                other_tenant_id=other_tenant.id,
                collection_id=collection.id,
                document_id=documents[0].id,
                ambiguous_document_ids=(documents[1].id, documents[2].id),
                other_document_id=other_document.id,
                version_id=versions[0].id,
                network_section_id=network.id,
                chunk_ids=tuple(item.id for item in runbook_chunks),
                repeated_chunk_ids=(repeated_chunks[0].id, repeated_chunks[1].id),
                other_chunk_id=secret_chunk.id,
            ),
        )


def _section(
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    ordinal: int,
    level: int,
    heading: str,
    path: str,
    *,
    parent_id: uuid.UUID | None = None,
) -> Section:
    return Section(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        document_id=document_id,
        document_version_id=version_id,
        parent_id=parent_id,
        ordinal=ordinal,
        level=level,
        heading_original=heading,
        heading_lexical=normalize_lexical(heading),
        page_start=1,
        page_end=6,
        path_original=path,
        path_lexical=normalize_lexical(path),
        source_metadata={},
    )


def _chunk(
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    section_id: uuid.UUID,
    chunk_index: int,
    page: int,
    text: str,
) -> Chunk:
    return Chunk(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        document_id=document_id,
        document_version_id=version_id,
        section_id=section_id,
        occurrence_index=0,
        chunk_index=chunk_index,
        page_start=page,
        page_end=page,
        char_start=0,
        char_end=len(text),
        content_sha256=uuid.uuid5(uuid.NAMESPACE_URL, text).hex * 2,
        lexical_sha256=uuid.uuid5(uuid.NAMESPACE_DNS, normalize_lexical(text)).hex * 2,
        token_count=len(text.split()),
        text_original=text,
        text_lexical=normalize_lexical(text),
    )
