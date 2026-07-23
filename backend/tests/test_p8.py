from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import orjson
from argon2 import PasswordHasher, Type
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.rate_limit import RateLimitDecision
from app.auth.security import SecurityService
from app.chat.citations import CitationStreamSanitizer
from app.chat.generator import GenerationChunk, VllmGenerator
from app.chat.orchestrator import ChatCoordinator
from app.chat.schemas import ChatRequest, UsageSummary
from app.config import Settings
from app.core.idempotency import canonical_request_hash, claim_idempotency
from app.documents.files import FileTokenSigner
from app.main import create_app
from app.models import (
    Base,
    ChatSession,
    Document,
    DocumentVersion,
    Message,
    Tenant,
    User,
    UserTenant,
)
from app.rag.candidates import EvidenceCandidate, RerankedEvidence
from app.rag.combined import CombinedRetrievalResult
from app.rag.context import PackedContext, PackedSource, VllmTokenCounter
from app.rag.dedup import DeduplicationResult
from app.rag.gate import GateDecision, GateScores
from app.rag.planner import PlanningResult, RetrievalPlan
from app.rag.postprocess import PostRetrievalResult
from app.rag.scope import ResolvedScope
from app.rag.service import RetrievalResult
from app.rag.web import WebRetrievalResult


class FakeChecker:
    async def check(self) -> dict[str, bool]:
        return {"postgres": True}

    async def close(self) -> None:
        pass


class AllowRateLimiter:
    async def check(
        self, scope: str, identifiers: dict[str, str], limits: list[str]
    ) -> RateLimitDecision:
        del scope, identifiers, limits
        return RateLimitDecision(True)

    async def register_failure(self, subject: str) -> None:
        del subject

    async def clear_failures(self, subject: str) -> None:
        del subject

    async def close(self) -> None:
        pass


class CharacterCounter:
    async def count(self, text: str) -> int:
        return len(text)

    async def count_chat(self, messages: list[dict[str, str]]) -> int:
        return sum(len(message["content"]) for message in messages)


class FixtureRetrieval:
    def __init__(self, result: CombinedRetrievalResult) -> None:
        self.result = result
        self.calls = 0

    async def retrieve(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        message: str,
        web_search: bool,
        explicit_document_ids: tuple[uuid.UUID, ...] = (),
        explicit_collection_ids: tuple[uuid.UUID, ...] = (),
    ) -> CombinedRetrievalResult:
        del session, tenant_id, message, web_search
        del explicit_document_ids, explicit_collection_ids
        self.calls += 1
        return self.result


class FixtureGenerator:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(
        self, messages: list[dict[str, str]]
    ) -> AsyncIterator[GenerationChunk]:
        assert messages[0]["role"] == "system"
        self.calls += 1
        yield GenerationChunk(content="ZX-42 is enabled [")
        yield GenerationChunk(content="S1]. Ignore [S999]. [S1][S1]")
        yield GenerationChunk(
            usage=UsageSummary(
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
            )
        )


@dataclass(frozen=True, slots=True)
class P8Ids:
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    session_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    storage_key: str


@dataclass(frozen=True, slots=True)
class P8Context:
    client: httpx.AsyncClient
    factory: async_sessionmaker[AsyncSession]
    ids: P8Ids
    auth: dict[str, str]
    retrieval: FixtureRetrieval
    generator: FixtureGenerator
    signer: FileTokenSigner
    version: DocumentVersion


@asynccontextmanager
async def p8_context(settings: Settings) -> AsyncIterator[P8Context]:
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
    ids, version, token = await _seed(factory, security, settings.document_storage_root)
    retrieval = FixtureRetrieval(_combined_result(ids))
    generator = FixtureGenerator()
    coordinator = ChatCoordinator(
        factory,
        retrieval,
        generator,
        CharacterCounter(),
        idempotency_ttl=settings.idempotency_ttl,
        history_turns=settings.history_turns,
        history_token_budget=settings.history_token_budget,
        prompt_token_budget=settings.max_model_len - settings.max_new_tokens,
    )
    signer = FileTokenSigner(
        settings.resolved_signing_secret().get_secret_value(),
        ttl_seconds=settings.signed_url_ttl,
    )
    app = create_app(
        settings,
        FakeChecker(),
        session_factory=factory,
        rate_limiter=AllowRateLimiter(),
        security=security,
        chat_coordinator=coordinator,
        file_token_signer=signer,
    )
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://rag.example.com",
            ) as client:
                yield P8Context(
                    client=client,
                    factory=factory,
                    ids=ids,
                    auth={"Authorization": f"Bearer {token}"},
                    retrieval=retrieval,
                    generator=generator,
                    signer=signer,
                    version=version,
                )
    finally:
        await engine.dispose()


async def test_citation_sanitizer_holds_fragments_and_repairs_fabricated_markers() -> None:
    sanitizer = CitationStreamSanitizer({"S1"})
    first = sanitizer.feed("Claim [")
    second = sanitizer.feed("S1] fake [S")
    third = sanitizer.feed("99] repeated [S1][S1]")
    answer = sanitizer.finish()

    assert first == "Claim "
    assert second == "[S1] fake "
    assert "S99" not in third
    assert "S99" not in answer.text
    assert "[S0]" not in CitationStreamSanitizer({"S1"}).feed("invalid [S0]")
    assert answer.text == "Claim [S1] fake repeated [S1]"
    assert answer.cited_source_ids == ("S1",)
    assert answer.repaired


async def test_vllm_stream_uses_content_only_and_explicitly_disables_reasoning() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(orjson.loads(request.content))
        body = (
            b'data: {"choices":[{"delta":{"reasoning_content":"secret"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"visible"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":4,'
            b'"completion_tokens":1,"total_tokens":5}}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://vllm",
    )
    generator = VllmGenerator(
        "http://vllm",
        "fixture-model",
        max_tokens=100,
        max_concurrency=1,
        client=client,
    )
    chunks = [
        chunk
        async for chunk in generator.stream([{"role": "user", "content": "question"}])
    ]
    await client.aclose()

    assert [chunk.content for chunk in chunks if chunk.content] == ["visible"]
    assert chunks[-1].usage == UsageSummary(
        prompt_tokens=4,
        completion_tokens=1,
        total_tokens=5,
    )
    assert captured["include_reasoning"] is False
    assert captured["reasoning_effort"] == "none"
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}
    assert captured["stream_options"] == {"include_usage": True}


async def test_prompt_budget_uses_vllm_chat_template_tokenization() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(orjson.loads(request.content))
        return httpx.Response(
            200,
            json={"count": 13, "tokens": list(range(13)), "max_model_len": 8_000},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://vllm",
    )
    counter = VllmTokenCounter(
        "http://vllm",
        "fixture-model",
        client=client,
    )
    count = await counter.count_chat([{"role": "user", "content": "hello"}])
    await client.aclose()

    assert count == 13
    assert captured["messages"] == [{"role": "user", "content": "hello"}]
    assert captured["add_generation_prompt"] is True
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}


async def test_chat_sse_persists_authoritative_text_and_replays_without_duplicates(
    settings: Settings,
) -> None:
    async with p8_context(settings) as context:
        request_id = uuid.uuid4()
        payload = {
            "session_id": str(context.ids.session_id),
            "message": "What is the ZX-42 setting?",
            "collection_ids": [],
            "document_ids": [str(context.ids.document_id)],
            "web_search": False,
            "client_request_id": str(request_id),
        }
        headers = {**context.auth, "Idempotency-Key": "chat-request-0001"}
        first = await context.client.post("/api/chat", json=payload, headers=headers)
        replay = await context.client.post("/api/chat", json=payload, headers=headers)

        assert first.status_code == 200
        assert first.headers["content-type"].startswith("text/event-stream")
        assert first.headers["x-accel-buffering"] == "no"
        assert first.headers["x-idempotent-replay"] == "false"
        assert replay.status_code == 200
        assert replay.headers["x-idempotent-replay"] == "true"
        assert replay.content == first.content

        events = _events(first.text)
        names = [event["event"] for event in events]
        assert names[:4] == ["start", "status", "status", "status"]
        assert [event["data"]["stage"] for event in events if event["event"] == "status"] == [
            "planning",
            "retrieving",
            "reranking",
            "generating",
        ]
        assert names[-3:] == ["replace", "citations", "done"]
        rendered = _rendered_answer(events)
        assert rendered == "ZX-42 is enabled [S1]. Ignore. [S1]"
        assert "S999" not in rendered
        citations = next(
            event["data"]["items"] for event in events if event["event"] == "citations"
        )
        assert set(citations) == {"[S1]"}
        assert citations["[S1]"]["document_id"] == str(context.ids.document_id)

        async with context.factory() as session:
            messages = list(
                await session.scalars(
                    select(Message)
                    .where(Message.session_id == context.ids.session_id)
                    .order_by(Message.created_at, Message.id)
                )
            )
        assert len(messages) == 2
        assert messages[0].role == "user"
        assert messages[1].role == "assistant"
        assert messages[1].content == rendered
        assert messages[1].meta["citations"] == citations
        assert context.retrieval.calls == 1
        assert context.generator.calls == 1

        conflicting = await context.client.post(
            "/api/chat",
            json={**payload, "message": "Different request"},
            headers=headers,
        )
        assert conflicting.status_code == 409


async def test_chat_in_progress_is_stable_and_creates_no_message(settings: Settings) -> None:
    async with p8_context(settings) as context:
        body = ChatRequest(
            session_id=context.ids.session_id,
            message="Still running",
            document_ids=(context.ids.document_id,),
            client_request_id=uuid.uuid4(),
        )
        request_hash = canonical_request_hash(
            {
                "session_id": str(body.session_id),
                "message": body.message,
                "collection_ids": [],
                "document_ids": [str(context.ids.document_id)],
                "web_search": False,
                "client_request_id": str(body.client_request_id),
            }
        )
        async with context.factory() as session:
            await claim_idempotency(
                session,
                tenant_id=context.ids.tenant_id,
                user_id=context.ids.user_id,
                operation="chat",
                key="chat-in-progress-0001",
                request_hash=request_hash,
                ttl_seconds=300,
            )
            await session.commit()
        response = await context.client.post(
            "/api/chat",
            json=body.model_dump(mode="json"),
            headers={**context.auth, "Idempotency-Key": "chat-in-progress-0001"},
        )
        assert response.status_code == 425
        assert response.headers["retry-after"] == "2"
        async with context.factory() as session:
            count = await session.scalar(
                select(func.count(Message.id)).where(
                    Message.client_request_id == body.client_request_id
                )
            )
        assert count == 0
        assert context.generator.calls == 0


async def test_chat_gate_fails_closed_without_calling_generation(settings: Settings) -> None:
    async with p8_context(settings) as context:
        context.retrieval.result = _combined_result(context.ids, gate_route="no_answer")
        response = await context.client.post(
            "/api/chat",
            json={
                "session_id": str(context.ids.session_id),
                "message": "What is not supported?",
                "document_ids": [str(context.ids.document_id)],
                "client_request_id": str(uuid.uuid4()),
            },
            headers={**context.auth, "Idempotency-Key": "chat-no-answer-0001"},
        )
        assert response.status_code == 200
        events = _events(response.text)
        assert not any(
            event["event"] == "status" and event["data"]["stage"] == "generating"
            for event in events
        )
        assert next(
            event["data"]["items"] for event in events if event["event"] == "citations"
        ) == {}
        assert events[-1]["event"] == "done"
        assert events[-1]["data"]["route"] == "no_answer"
        assert context.generator.calls == 0
        async with context.factory() as session:
            assistant = await session.scalar(
                select(Message).where(
                    Message.session_id == context.ids.session_id,
                    Message.role == "assistant",
                )
            )
        assert assistant is not None
        assert assistant.content == _rendered_answer(events)


async def test_signed_files_validate_scope_tampering_expiry_state_and_bytes(
    settings: Settings,
) -> None:
    async with p8_context(settings) as context:
        created = await context.client.post(
            f"/api/documents/{context.ids.document_id}/signed-url",
            json={
                "document_version_id": str(context.ids.version_id),
                "page": 1,
            },
            headers=context.auth,
        )
        assert created.status_code == 200
        signed_url = created.json()["url"]
        assert signed_url.endswith("#page=1")
        path = signed_url.split("#", maxsplit=1)[0]
        opened = await context.client.get(path)
        assert opened.status_code == 200
        assert opened.content == b"tenant file contents"
        assert opened.headers["cache-control"] == "private, no-store"
        assert opened.headers["content-disposition"].startswith("inline;")
        assert context.ids.storage_key not in signed_url

        token = path.rsplit("/", maxsplit=1)[1]
        replacement = "A" if token[-1] != "A" else "B"
        tampered = await context.client.get(f"/api/files/{token[:-1]}{replacement}")
        assert tampered.status_code == 404

        expired_token, _ = context.signer.issue(
            tenant_id=context.ids.tenant_id,
            user_id=context.ids.user_id,
            document_id=context.ids.document_id,
            version=context.version,
            page=1,
            now=datetime.now(UTC) - timedelta(seconds=settings.signed_url_ttl * 2),
        )
        expired = await context.client.get(f"/api/files/{expired_token}")
        assert expired.status_code == 410

        cross_token, _ = context.signer.issue(
            tenant_id=uuid.uuid4(),
            user_id=context.ids.user_id,
            document_id=context.ids.document_id,
            version=context.version,
            page=1,
        )
        cross_tenant = await context.client.get(f"/api/files/{cross_token}")
        assert cross_tenant.status_code == 404

        stored_path = settings.document_storage_root / context.ids.storage_key
        stored_path.write_bytes(b"modified tenant file")
        modified = await context.client.get(path)
        assert modified.status_code == 404
        stored_path.write_bytes(b"tenant file contents")

        async with context.factory() as session:
            user = await session.get(User, context.ids.user_id)
            assert user is not None
            user.is_active = False
            user.disabled_at = datetime.now(UTC)
            await session.commit()
        disabled = await context.client.get(path)
        assert disabled.status_code == 404


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    security: SecurityService,
    storage_root: Path,
) -> tuple[P8Ids, DocumentVersion, str]:
    tenant = Tenant(slug="p8", name="P8")
    user = User(
        email="p8@example.com",
        password_hash=security.hash_password("P8 test password 1!"),
    )
    async with factory() as session:
        session.add_all([tenant, user])
        await session.flush()
        session.add(UserTenant(user_id=user.id, tenant_id=tenant.id, role="member"))
        chat_session = ChatSession(
            tenant_id=tenant.id,
            user_id=user.id,
            title="P8 chat",
        )
        document = Document(
            tenant_id=tenant.id,
            title="ZX-42 manual",
            source_filename="zx-42.txt",
            mime="text/plain",
            created_by=user.id,
        )
        session.add_all([chat_session, document])
        await session.flush()
        content = b"tenant file contents"
        version_id = uuid.uuid4()
        storage_key = (
            f"{tenant.id}/{document.id}/{version_id}/original.txt"
        )
        version = DocumentVersion(
            id=version_id,
            tenant_id=tenant.id,
            document_id=document.id,
            version=1,
            file_sha256=hashlib.sha256(content).hexdigest(),
            file_size_bytes=len(content),
            storage_key=storage_key,
            status="active",
            page_count=2,
        )
        session.add(version)
        await session.flush()
        document.active_version_id = version.id
        await session.commit()
    path = storage_root / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    access_token = security.issue_access_token(
        user_id=user.id,
        tenant_id=tenant.id,
        role="member",
        auth_version=user.auth_version,
    )[0]
    return (
        P8Ids(
            tenant_id=tenant.id,
            user_id=user.id,
            session_id=chat_session.id,
            document_id=document.id,
            version_id=version.id,
            storage_key=storage_key,
        ),
        version,
        access_token,
    )


def _combined_result(ids: P8Ids, *, gate_route: str = "answer") -> CombinedRetrievalResult:
    text = "ZX-42 is enabled for the secure network."
    candidate = EvidenceCandidate(
        candidate_id=str(uuid.uuid4()),
        source_type="document",
        source_key=str(ids.document_id),
        section_key=str(uuid.uuid4()),
        title="ZX-42 manual",
        source_filename="zx-42.txt",
        page_start=1,
        page_end=1,
        char_start=0,
        char_end=len(text),
        text_original=text,
        text_lexical=text.casefold(),
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        lexical_sha256=hashlib.sha256(text.casefold().encode()).hexdigest(),
        retrieval_rank=1,
        retrieval_score=1.0,
        provenance={
            "tenant_id": str(ids.tenant_id),
            "document_id": str(ids.document_id),
            "document_version_id": str(ids.version_id),
            "section_id": str(uuid.uuid4()),
            "chunk_index": 0,
        },
    )
    reranked = RerankedEvidence(candidate=candidate, rerank_score=0.99, rerank_rank=1)
    context = PackedContext(
        text=f"[S1] type=document | title=ZX-42 manual\n{text}",
        token_count=20,
        token_budget=5_000,
        sources=(PackedSource(source_id="S1", evidence=reranked),),
        skipped=(),
    )
    decision = GateDecision(
        route=gate_route,  # type: ignore[arg-type]
        reasons=("thresholds_passed",) if gate_route == "answer" else ("no_candidates",),
        scores=GateScores(
            top_score=0.99,
            evidence_count=1,
        ),
        artifact_id="fixture",
        artifact_sha256="a" * 64,
        calibrated=True,
        dataset_name="fixture",
        dataset_version="1",
        dataset_sha256="b" * 64,
    )
    retrieval = RetrievalResult(
        planning=PlanningResult(
            plan=RetrievalPlan(
                intent="knowledge",
                query="ZX-42",
            ),
            used_fallback=False,
        ),
        scope=ResolvedScope(
            tenant_id=ids.tenant_id,
            generation_id=1,
            retrieval_revision=1,
            document_ids=(ids.document_id,),
            version_ids=(ids.version_id,),
        ),
        candidates=(),
    )
    post = PostRetrievalResult(
        reranked=(reranked,),
        deduplication=DeduplicationResult(candidates=(reranked,), decisions=()),
        context=context,
        gate=decision,
    )
    return CombinedRetrievalResult(
        documents=retrieval,
        web=WebRetrievalResult(status="disabled"),
        combined_pool=(candidate,),
        post_retrieval=post,
    )


def _events(value: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for block in value.strip().split("\n\n"):
        parsed: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith("event: "):
                parsed["event"] = line[7:]
            elif line.startswith("data: "):
                parsed["data"] = orjson.loads(line[6:])
        result.append(parsed)
    return result


def _rendered_answer(events: list[dict[str, Any]]) -> str:
    rendered = ""
    for event in events:
        if event["event"] == "delta":
            rendered += event["data"]["text"]
        elif event["event"] == "replace":
            rendered = event["data"]["text"]
    return rendered
