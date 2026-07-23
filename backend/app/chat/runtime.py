from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.generator import VllmGenerator
from app.chat.orchestrator import ChatCoordinator
from app.config import Settings
from app.core.metrics import Metrics
from app.ingest.embedder import TeiClient
from app.rag.cache import RedisJsonCache
from app.rag.combined import CombinedRetrievalService
from app.rag.context import ContextPacker, VllmTokenCounter
from app.rag.gate import ConfidenceGate, load_gate_artifact
from app.rag.planner import VllmPlanner
from app.rag.postprocess import PostRetrievalService
from app.rag.reranker import TeiReranker
from app.rag.retriever import HybridRetriever, QdrantHybridSearch
from app.rag.scope import MetadataResolver
from app.rag.service import RetrievalService
from app.rag.web import InternalPageFetcher, SearxngClient, WebRetriever


class Closable(Protocol):
    async def close(self) -> None: ...


@dataclass(slots=True)
class ChatRuntime:
    coordinator: ChatCoordinator
    resources: tuple[Closable, ...]

    async def close(self) -> None:
        for resource in reversed(self.resources):
            await resource.close()


def build_chat_runtime(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    metrics: Metrics,
) -> ChatRuntime:
    cache = RedisJsonCache(str(settings.redis_url))
    qdrant = QdrantHybridSearch(str(settings.qdrant_url), settings.qdrant_collection)
    document_retrieval = RetrievalService(
        VllmPlanner(str(settings.vllm_base_url), settings.vllm_model),
        MetadataResolver(),
        TeiClient(str(settings.embed_url), expected_dimension=settings.embed_dim),
        HybridRetriever(
            qdrant,
            dense_limit=settings.retrieve_dense_n,
            sparse_limit=settings.retrieve_sparse_n,
            pool_limit=settings.rerank_pool_n,
            section_chunk_limit=settings.section_chunk_limit,
            neighbor_radius=settings.section_neighbor_radius,
        ),
        cache=cache,
        plan_cache_ttl=settings.cache_plan_ttl,
        retrieval_cache_ttl=settings.cache_retrieval_ttl,
        planner_revision=settings.vllm_model_revision,
        embedding_revision=settings.embed_revision,
    )
    search = SearxngClient(
        str(settings.searxng_url),
        result_limit=settings.web_top_results,
        timeout_seconds=settings.web_fetch_timeout,
    )
    page_fetcher = InternalPageFetcher(
        str(settings.web_fetcher_url),
        timeout_seconds=settings.web_fetch_timeout,
    )
    web = WebRetriever(
        search,
        page_fetcher,
        concurrency=settings.web_fetch_concurrency,
    )
    reranker = TeiReranker(
        str(settings.rerank_url),
        model_revision=settings.rerank_revision,
        max_candidates=settings.rerank_pool_n,
        cache=cache,
        cache_ttl=settings.cache_rerank_ttl,
    )
    token_counter = VllmTokenCounter(
        str(settings.vllm_base_url),
        settings.vllm_model,
    )
    gate_path = settings.retrieval_gate_config
    if not gate_path.is_absolute() and not gate_path.exists():
        gate_path = Path(__file__).resolve().parents[2] / gate_path
    gate = ConfidenceGate(
        load_gate_artifact(gate_path),
        embedding_model=settings.embed_model,
        embedding_revision=settings.embed_revision,
        reranker_model=settings.rerank_model,
        reranker_revision=settings.rerank_revision,
    )
    post = PostRetrievalService(
        reranker,
        ContextPacker(
            token_counter,
            token_budget=settings.context_token_budget,
            max_candidates=settings.rerank_keep,
            section_limit=settings.section_chunk_limit,
            source_limit=settings.document_chunk_limit,
            domain_limit=settings.domain_chunk_limit,
            web_limit=settings.web_context_limit,
        ),
        gate,
    )
    combined = CombinedRetrievalService(
        document_retrieval,
        web,
        post,
        pool_limit=settings.rerank_pool_n,
        metrics=metrics,
    )
    generator = VllmGenerator(
        str(settings.vllm_base_url),
        settings.vllm_model,
        max_tokens=settings.max_new_tokens,
        max_concurrency=settings.max_generation_concurrency,
    )
    coordinator = ChatCoordinator(
        session_factory,
        combined,
        generator,
        token_counter,
        idempotency_ttl=settings.idempotency_ttl,
        history_turns=settings.history_turns,
        history_token_budget=settings.history_token_budget,
        prompt_token_budget=settings.max_model_len - settings.max_new_tokens,
        metrics=metrics,
    )
    return ChatRuntime(
        coordinator=coordinator,
        resources=(cache, qdrant, search, page_fetcher, reranker, token_counter, generator),
    )
