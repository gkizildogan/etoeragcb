from __future__ import annotations

import asyncio
import hashlib

import orjson

from app.config import get_settings
from app.ingest.normalization import normalize_lexical
from app.rag.candidates import EvidenceCandidate
from app.rag.combined import merge_candidate_pool
from app.rag.context import ContextPacker, VllmTokenCounter
from app.rag.gate import ConfidenceGate, load_gate_artifact
from app.rag.postprocess import PostRetrievalService
from app.rag.reranker import TeiReranker
from app.rag.web import (
    InternalPageFetcher,
    SearxngClient,
    WebRetrievalError,
    WebRetriever,
)


async def run() -> dict[str, int | str]:
    settings = get_settings()
    search = SearxngClient(
        str(settings.searxng_url),
        result_limit=settings.web_top_results,
        timeout_seconds=settings.web_fetch_timeout,
    )
    page_fetcher = InternalPageFetcher(
        str(settings.web_fetcher_url), timeout_seconds=settings.web_fetch_timeout
    )
    reranker = TeiReranker(
        str(settings.rerank_url),
        model_revision=settings.rerank_revision,
        max_candidates=settings.rerank_pool_n,
        timeout_seconds=60,
    )
    token_counter = VllmTokenCounter(str(settings.vllm_base_url), settings.vllm_model)
    try:
        blocked_code = await _verify_private_target_is_blocked(page_fetcher)
        web = await WebRetriever(
            search,
            page_fetcher,
            concurrency=settings.web_fetch_concurrency,
        ).retrieve("site:docs.python.org asyncio open_connection Python")
        if not web.candidates:
            failure_codes = ",".join(failure.code for failure in web.failures)
            raise RuntimeError(
                f"live web branch produced no safe page: {web.status}:{failure_codes}"
            )
        document = _document_candidate()
        combined = merge_candidate_pool((document,), web.candidates, limit=settings.rerank_pool_n)
        gate = ConfidenceGate(
            load_gate_artifact(settings.retrieval_gate_config),
            embedding_model=settings.embed_model,
            embedding_revision=settings.embed_revision,
            reranker_model=settings.rerank_model,
            reranker_revision=settings.rerank_revision,
        )
        result = await PostRetrievalService(
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
        ).process(
            query="Python asyncio open_connection",
            candidates=combined,
        )
        source_types = {source.evidence.candidate.source_type for source in result.context.sources}
        if source_types != {"document", "web"}:
            raise RuntimeError("live combined context did not retain both source types")
        if result.context.token_count > settings.context_token_budget:
            raise RuntimeError("live P7 context exceeded its token budget")
        return {
            "status": "ok",
            "web_status": web.status,
            "safe_web_candidates": len(web.candidates),
            "web_failures": len(web.failures),
            "packed_sources": len(result.context.sources),
            "context_tokens": result.context.token_count,
            "private_target": blocked_code,
            "gate_reason": result.gate.reasons[0],
        }
    finally:
        await search.close()
        await page_fetcher.close()
        await reranker.close()
        await token_counter.close()


async def _verify_private_target_is_blocked(fetcher: InternalPageFetcher) -> str:
    try:
        await fetcher.fetch("http://127.0.0.1/")
    except WebRetrievalError as exc:
        if exc.code != "forbidden_address":
            raise RuntimeError("private target failed for an unexpected reason") from exc
        return exc.code
    raise RuntimeError("private target was not blocked")


def _document_candidate() -> EvidenceCandidate:
    text = "Python asyncio open_connection creates a network stream connection."
    lexical = normalize_lexical(text)
    return EvidenceCandidate(
        candidate_id="p7-smoke-document",
        source_type="document",
        source_key="p7-smoke-document",
        section_key="p7-smoke-section",
        title="P7 local document smoke",
        source_filename="p7-smoke.txt",
        page_start=1,
        page_end=1,
        char_start=0,
        char_end=len(text),
        text_original=text,
        text_lexical=lexical,
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        lexical_sha256=hashlib.sha256(lexical.encode()).hexdigest(),
        retrieval_rank=1,
        retrieval_score=1.0,
    )


def main() -> None:
    print(orjson.dumps(asyncio.run(run())).decode())


if __name__ == "__main__":
    main()
