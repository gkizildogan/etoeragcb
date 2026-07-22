from __future__ import annotations

import asyncio
import hashlib

import orjson

from app.config import get_settings
from app.ingest.normalization import normalize_lexical
from app.rag.candidates import EvidenceCandidate
from app.rag.context import ContextPacker, VllmTokenCounter
from app.rag.gate import ConfidenceGate, load_gate_artifact
from app.rag.postprocess import PostRetrievalService
from app.rag.reranker import TeiReranker


async def run() -> dict[str, int | str]:
    settings = get_settings()
    reranker = TeiReranker(
        str(settings.rerank_url),
        model_revision=settings.rerank_revision,
        max_candidates=settings.rerank_pool_n,
        cache_ttl=0,
    )
    token_counter = VllmTokenCounter(str(settings.vllm_base_url), settings.vllm_model)
    gate = ConfidenceGate(
        load_gate_artifact(settings.retrieval_gate_config),
        embedding_model=settings.embed_model,
        embedding_revision=settings.embed_revision,
        reranker_model=settings.rerank_model,
        reranker_revision=settings.rerank_revision,
    )
    service = PostRetrievalService(
        reranker,
        ContextPacker(
            token_counter,
            token_budget=settings.context_token_budget,
            max_candidates=settings.rerank_keep,
            section_limit=settings.section_chunk_limit,
            source_limit=settings.document_chunk_limit,
            domain_limit=settings.domain_chunk_limit,
        ),
        gate,
    )
    candidates = (
        _candidate(
            "relevant-tr",
            "ZX-42 ağ ayar\u0131 güvenli VLAN üzerinde etkinleştirilir.",
            rank=1,
            exact_terms=("ZX-42",),
        ),
        _candidate(
            "relevant-en",
            "The ZX-42 network setting is enabled on the secure VLAN.",
            rank=2,
            exact_terms=("ZX-42",),
        ),
        _candidate(
            "distractor",
            "This unrelated paragraph describes cafeteria opening hours.",
            rank=3,
        ),
    )
    try:
        result = await service.process(
            query="ZX-42 ağ ayar\u0131 / network setting",
            candidates=candidates,
        )
        if result.reranked[0].candidate.candidate_id not in {"relevant-tr", "relevant-en"}:
            raise RuntimeError("live reranker did not rank bilingual relevant evidence first")
        if result.context.token_count > settings.context_token_budget:
            raise RuntimeError("live context exceeded the serving-tokenizer budget")
        if result.gate.reasons != ("calibration_unavailable",):
            raise RuntimeError("uncalibrated production gate did not fail closed")
        return {
            "status": "ok",
            "top_candidate": result.reranked[0].candidate.candidate_id,
            "packed_sources": len(result.context.sources),
            "context_tokens": result.context.token_count,
            "gate_route": result.gate.route,
            "gate_reason": result.gate.reasons[0],
        }
    finally:
        await reranker.close()
        await token_counter.close()


def _candidate(
    candidate_id: str,
    text: str,
    *,
    rank: int,
    exact_terms: tuple[str, ...] = (),
) -> EvidenceCandidate:
    lexical = normalize_lexical(text)
    return EvidenceCandidate(
        candidate_id=candidate_id,
        source_type="document",
        source_key=f"document-{rank}",
        section_key=f"section-{rank}",
        title=f"P6 smoke source {rank}",
        source_filename=f"p6-smoke-{rank}.txt",
        page_start=1,
        page_end=1,
        char_start=0,
        char_end=len(text),
        text_original=text,
        text_lexical=lexical,
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        lexical_sha256=hashlib.sha256(lexical.encode()).hexdigest(),
        retrieval_rank=rank,
        retrieval_score=1 / (60 + rank),
        matched_exact_terms=exact_terms,
    )


def main() -> None:
    print(orjson.dumps(asyncio.run(run())).decode())


if __name__ == "__main__":
    main()
