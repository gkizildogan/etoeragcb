from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from time import perf_counter
from typing import Protocol

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.dependencies import Principal
from app.chat.citations import CitationStreamSanitizer, citations_for_answer
from app.chat.generator import GenerationChunk, GenerationError
from app.chat.prompts import generation_messages
from app.chat.schemas import (
    AcceptedChat,
    ChatRequest,
    StoredChatReplay,
    StreamEvent,
    UsageSummary,
)
from app.core.idempotency import (
    ClaimState,
    canonical_request_hash,
    claim_idempotency,
    complete_idempotency,
    fail_idempotency,
)
from app.core.metrics import Metrics
from app.models import ChatSession, Message, User
from app.rag.combined import CombinedRetrievalResult
from app.rag.context import PackedContext, TokenCounter
from app.rag.scope import ScopeValidationError

logger = structlog.get_logger(__name__)
CHAT_OPERATION = "chat"


class CombinedRetriever(Protocol):
    async def retrieve(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        message: str,
        web_search: bool,
        explicit_document_ids: tuple[uuid.UUID, ...] = (),
        explicit_collection_ids: tuple[uuid.UUID, ...] = (),
    ) -> CombinedRetrievalResult: ...


class ContentGenerator(Protocol):
    def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[GenerationChunk]: ...


class ChatTokenCounter(TokenCounter, Protocol):
    async def count_chat(self, messages: list[dict[str, str]]) -> int: ...


class ChatConflictError(Exception):
    pass


class ChatInProgressError(Exception):
    pass


class ChatSessionNotFoundError(Exception):
    pass


class ChatCoordinator:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        retrieval: CombinedRetriever,
        generator: ContentGenerator,
        token_counter: ChatTokenCounter,
        *,
        idempotency_ttl: int,
        history_turns: int,
        history_token_budget: int,
        prompt_token_budget: int,
        metrics: Metrics | None = None,
    ) -> None:
        self._sessions = session_factory
        self._retrieval = retrieval
        self._generator = generator
        self._token_counter = token_counter
        self._idempotency_ttl = idempotency_ttl
        self._history_turns = history_turns
        self._history_token_budget = history_token_budget
        if prompt_token_budget < 1:
            raise ValueError("prompt token budget must be positive")
        self._prompt_token_budget = prompt_token_budget
        self._metrics = metrics

    async def accept(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        request: ChatRequest,
        idempotency_key: str,
    ) -> AcceptedChat:
        owned = await session.scalar(
            select(ChatSession).where(
                ChatSession.id == request.session_id,
                ChatSession.tenant_id == principal.tenant_id,
                ChatSession.user_id == principal.user_id,
                ChatSession.deleted_at.is_(None),
            )
        )
        if owned is None:
            raise ChatSessionNotFoundError
        request_hash = canonical_request_hash(
            {
                "session_id": str(request.session_id),
                "message": request.message,
                "collection_ids": sorted(str(item) for item in request.collection_ids),
                "document_ids": sorted(str(item) for item in request.document_ids),
                "web_search": request.web_search,
                "client_request_id": str(request.client_request_id),
            }
        )
        claim = await claim_idempotency(
            session,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            operation=CHAT_OPERATION,
            key=idempotency_key,
            request_hash=request_hash,
            ttl_seconds=self._idempotency_ttl,
        )
        if claim.state is ClaimState.CONFLICT:
            await session.rollback()
            raise ChatConflictError
        if claim.state is ClaimState.IN_PROGRESS:
            await session.rollback()
            raise ChatInProgressError
        if claim.state is ClaimState.REPLAY:
            try:
                replay = StoredChatReplay.model_validate(claim.response)
            except ValidationError as exc:
                await session.rollback()
                raise RuntimeError("stored chat replay is invalid") from exc
            user_message_id = _user_message_id(replay)
            await session.rollback()
            return AcceptedChat(
                request=request,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                user_message_id=user_message_id,
                idempotency_key=idempotency_key,
                replay=replay,
            )

        existing = await session.scalar(
            select(Message).where(
                Message.tenant_id == principal.tenant_id,
                Message.user_id == principal.user_id,
                Message.client_request_id == request.client_request_id,
            )
        )
        if existing is not None and (
            claim.state is ClaimState.CLAIMED
            or existing.role != "user"
            or existing.session_id != request.session_id
            or existing.content != request.message
        ):
            await session.rollback()
            raise ChatConflictError
        user_message = existing or Message(
            tenant_id=principal.tenant_id,
            session_id=request.session_id,
            user_id=principal.user_id,
            role="user",
            content=request.message,
            meta={
                "collection_ids": [str(item) for item in request.collection_ids],
                "document_ids": [str(item) for item in request.document_ids],
                "web_search": request.web_search,
            },
            client_request_id=request.client_request_id,
        )
        if existing is None:
            session.add(user_message)
        owned.updated_at = datetime.now(UTC)
        await session.flush()
        await session.commit()
        return AcceptedChat(
            request=request,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            user_message_id=user_message.id,
            idempotency_key=idempotency_key,
        )

    async def events(self, accepted: AcceptedChat) -> AsyncIterator[StreamEvent]:
        if accepted.replay is not None:
            for event in accepted.replay.events:
                yield event
            return

        completed = False
        transcript: list[StreamEvent] = []
        try:
            start = StreamEvent(
                event="start",
                data={
                    "request_id": str(accepted.request.client_request_id),
                    "user_message_id": str(accepted.user_message_id),
                    "session_id": str(accepted.request.session_id),
                    "options": {
                        "collection_ids": [str(item) for item in accepted.request.collection_ids],
                        "document_ids": [str(item) for item in accepted.request.document_ids],
                        "web_search": accepted.request.web_search,
                    },
                },
            )
            transcript.append(start)
            yield start
            for stage in ("planning", "retrieving", "reranking"):
                status_event = StreamEvent(event="status", data={"stage": stage})
                transcript.append(status_event)
                yield status_event

            retrieval_started = perf_counter()
            async with self._sessions() as session:
                retrieval = await self._retrieval.retrieve(
                    session,
                    tenant_id=accepted.tenant_id,
                    message=accepted.request.message,
                    web_search=accepted.request.web_search,
                    explicit_document_ids=accepted.request.document_ids,
                    explicit_collection_ids=accepted.request.collection_ids,
                )
                history = await self._history(
                    session,
                    accepted=accepted,
                )
            if self._metrics is not None:
                self._metrics.chat_stage_duration.labels("retrieval").observe(
                    perf_counter() - retrieval_started
                )

            intent = retrieval.documents.planning.plan.intent
            packed = retrieval.post_retrieval.context
            grounded = intent == "knowledge"
            route = "answer"
            usage = UsageSummary()
            citations: dict[str, object] = {}
            final_text: str

            if grounded and retrieval.post_retrieval.gate.route != "answer":
                route = "no_answer"
                final_text = _no_answer(accepted.request.message)
                delta = StreamEvent(event="delta", data={"text": final_text})
                transcript.append(delta)
                yield delta
            else:
                generating = StreamEvent(event="status", data={"stage": "generating"})
                transcript.append(generating)
                yield generating
                messages = await self._bounded_generation_messages(
                    question=accepted.request.message,
                    context=packed,
                    history=history,
                    grounded=grounded,
                )
                sanitizer = CitationStreamSanitizer(
                    {source.source_id for source in packed.sources} if grounded else set()
                )
                generation_started = perf_counter()
                async for chunk in self._generator.stream(messages):
                    if chunk.usage is not None:
                        usage = chunk.usage
                    if not chunk.content:
                        continue
                    safe = sanitizer.feed(chunk.content)
                    if safe:
                        delta = StreamEvent(event="delta", data={"text": safe})
                        transcript.append(delta)
                        yield delta
                answer = sanitizer.finish()
                if self._metrics is not None:
                    self._metrics.chat_stage_duration.labels("generation").observe(
                        perf_counter() - generation_started
                    )
                    if answer.repaired:
                        self._metrics.citation_repairs.inc()
                final_text = answer.text
                if grounded and not answer.cited_source_ids:
                    final_text = _no_answer(accepted.request.message)
                    route = "no_answer"
                    citations = {}
                else:
                    citations = {
                        marker: value.model_dump(mode="json")
                        for marker, value in citations_for_answer(answer, packed.sources).items()
                    }
                    if not grounded:
                        route = "conversation"
                if not final_text:
                    final_text = _no_answer(accepted.request.message)
                    route = "no_answer"
                    citations = {}
                if final_text != sanitizer.streamed_text:
                    replacement = StreamEvent(event="replace", data={"text": final_text})
                    transcript.append(replacement)
                    yield replacement

            assistant_id = uuid.uuid4()
            citations_event = StreamEvent(event="citations", data={"items": citations})
            done = StreamEvent(
                event="done",
                data={
                    "message_id": str(assistant_id),
                    "route": route,
                    "usage": usage.model_dump(mode="json"),
                },
            )
            transcript.extend((citations_event, done))
            persistence_started = perf_counter()
            await self._persist(
                accepted=accepted,
                assistant_id=assistant_id,
                answer=final_text,
                route=route,
                usage=usage,
                citations=citations,
                retrieval=retrieval,
                transcript=tuple(transcript),
            )
            completed = True
            if self._metrics is not None:
                self._metrics.chat_stage_duration.labels("persistence").observe(
                    perf_counter() - persistence_started
                )
            yield citations_event
            yield done
        except ScopeValidationError:
            await self._fail(accepted)
            yield _error("invalid_scope", retryable=False)
        except GenerationError as exc:
            await self._fail(accepted)
            yield _error(exc.code, retryable=exc.retryable)
        except Exception:
            await self._fail(accepted)
            logger.exception(
                "chat_stream_failed",
                tenant_id=str(accepted.tenant_id),
                request_id=str(accepted.request.client_request_id),
            )
            yield _error("chat_unavailable", retryable=True)
        except BaseException:
            if not completed:
                await self._fail(accepted)
            raise

    async def _history(
        self,
        session: AsyncSession,
        *,
        accepted: AcceptedChat,
    ) -> list[dict[str, str]]:
        if self._history_turns == 0 or self._history_token_budget == 0:
            return []
        limit = self._history_turns * 2
        rows = list(
            await session.scalars(
                select(Message)
                .where(
                    Message.tenant_id == accepted.tenant_id,
                    Message.session_id == accepted.request.session_id,
                    Message.user_id == accepted.user_id,
                    Message.id != accepted.user_message_id,
                    Message.role.in_(("user", "assistant")),
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(limit)
            )
        )
        selected: list[dict[str, str]] = []
        for message in rows:
            candidate = [{"role": message.role, "content": message.content}, *selected]
            serialized = "\n".join(f"{item['role']}: {item['content']}" for item in candidate)
            if await self._token_counter.count(serialized) > self._history_token_budget:
                continue
            selected = candidate
        return selected

    async def _bounded_generation_messages(
        self,
        *,
        question: str,
        context: PackedContext,
        history: list[dict[str, str]],
        grounded: bool,
    ) -> list[dict[str, str]]:
        bounded_history = list(history)
        while True:
            messages = generation_messages(
                question=question,
                context=context,
                history=bounded_history,
                grounded=grounded,
            )
            if await self._token_counter.count_chat(messages) <= self._prompt_token_budget:
                return messages
            if not bounded_history:
                raise GenerationError("prompt_too_large", retryable=False)
            bounded_history.pop(0)

    async def _persist(
        self,
        *,
        accepted: AcceptedChat,
        assistant_id: uuid.UUID,
        answer: str,
        route: str,
        usage: UsageSummary,
        citations: dict[str, object],
        retrieval: CombinedRetrievalResult,
        transcript: tuple[StreamEvent, ...],
    ) -> None:
        replay = StoredChatReplay(
            assistant_message_id=assistant_id,
            events=transcript,
        )
        async with self._sessions() as session:
            active_user = await session.scalar(
                select(User).where(
                    User.id == accepted.user_id,
                    User.is_active.is_(True),
                    User.disabled_at.is_(None),
                )
            )
            owned = await session.scalar(
                select(ChatSession).where(
                    ChatSession.id == accepted.request.session_id,
                    ChatSession.tenant_id == accepted.tenant_id,
                    ChatSession.user_id == accepted.user_id,
                    ChatSession.deleted_at.is_(None),
                )
            )
            if active_user is None or owned is None:
                raise RuntimeError("chat owner is no longer active")
            assistant = Message(
                id=assistant_id,
                tenant_id=accepted.tenant_id,
                session_id=accepted.request.session_id,
                user_id=accepted.user_id,
                role="assistant",
                content=answer,
                meta=_assistant_metadata(
                    accepted=accepted,
                    route=route,
                    usage=usage,
                    citations=citations,
                    retrieval=retrieval,
                ),
            )
            session.add(assistant)
            owned.updated_at = datetime.now(UTC)
            await complete_idempotency(
                session,
                tenant_id=accepted.tenant_id,
                user_id=accepted.user_id,
                operation=CHAT_OPERATION,
                key=accepted.idempotency_key,
                response=replay.model_dump(mode="json"),
                resource_id=assistant_id,
            )
            await session.commit()
        if self._metrics is not None:
            self._metrics.chat_results.labels(route).inc()
            self._metrics.generation_tokens.labels("prompt").inc(usage.prompt_tokens)
            self._metrics.generation_tokens.labels("completion").inc(usage.completion_tokens)

    async def _fail(self, accepted: AcceptedChat) -> None:
        try:
            async with self._sessions() as session:
                await fail_idempotency(
                    session,
                    tenant_id=accepted.tenant_id,
                    user_id=accepted.user_id,
                    operation=CHAT_OPERATION,
                    key=accepted.idempotency_key,
                )
                await session.commit()
        except Exception:
            logger.warning(
                "chat_idempotency_failure_update_failed",
                tenant_id=str(accepted.tenant_id),
                request_id=str(accepted.request.client_request_id),
            )


def _assistant_metadata(
    *,
    accepted: AcceptedChat,
    route: str,
    usage: UsageSummary,
    citations: dict[str, object],
    retrieval: CombinedRetrievalResult,
) -> dict[str, object]:
    post = retrieval.post_retrieval
    sources = []
    for packed in post.context.sources:
        evidence = packed.evidence
        candidate = evidence.candidate
        sources.append(
            {
                "source_id": packed.source_id,
                "candidate_id": candidate.candidate_id,
                "source_type": candidate.source_type,
                "source_key": candidate.source_key,
                "content_sha256": candidate.content_sha256,
                "retrieval_rank": candidate.retrieval_rank,
                "rerank_rank": evidence.rerank_rank,
                "rerank_score": evidence.rerank_score,
                "provenance": {
                    key: value
                    for key, value in candidate.provenance.items()
                    if key
                    in {
                        "document_id",
                        "document_version_id",
                        "section_id",
                        "chunk_index",
                        "search_rank",
                        "redirect_count",
                        "combined_pool_rank",
                    }
                },
            }
        )
    return {
        "client_request_id": str(accepted.request.client_request_id),
        "user_message_id": str(accepted.user_message_id),
        "route": route,
        "usage": usage.model_dump(mode="json"),
        "citations": citations,
        "retrieval": {
            "intent": retrieval.documents.planning.plan.intent,
            "planner_fallback": retrieval.documents.planning.used_fallback,
            "web_status": retrieval.web.status,
            "gate": post.gate.model_dump(mode="json"),
            "context_tokens": post.context.token_count,
            "sources": sources,
        },
    }


def _no_answer(message: str) -> str:
    lowered = f" {message.casefold()} "
    turkish = any(character in lowered for character in "çğıöşü") or any(
        word in lowered
        for word in (" nedir ", " nasıl ", " hangi ", " hakkında ")  # noqa: RUF001
    )
    if turkish:
        return "Bu soruyu yanıtlamak için yeterli güvenilir kanıt bulamadım."  # noqa: RUF001
    return "I could not find enough reliable evidence to answer that question."


def _error(code: str, *, retryable: bool) -> StreamEvent:
    return StreamEvent(event="error", data={"code": code, "retryable": retryable})


def _user_message_id(replay: StoredChatReplay) -> uuid.UUID:
    for event in replay.events:
        if event.event != "start":
            continue
        value = event.data.get("user_message_id")
        if isinstance(value, str):
            try:
                return uuid.UUID(value)
            except ValueError:
                break
    raise RuntimeError("stored chat replay has no user message")
