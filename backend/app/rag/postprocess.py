from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.rag.candidates import EvidenceCandidate, RerankedEvidence, document_evidence
from app.rag.context import ContextPacker, PackedContext
from app.rag.dedup import DeduplicationResult, deduplicate
from app.rag.gate import ConfidenceGate, GateDecision
from app.rag.reranker import Reranker
from app.rag.retriever import RetrievalCandidate


class PostRetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reranked: tuple[RerankedEvidence, ...]
    deduplication: DeduplicationResult
    context: PackedContext
    gate: GateDecision


class PostRetrievalService:
    def __init__(
        self,
        reranker: Reranker,
        context_packer: ContextPacker,
        confidence_gate: ConfidenceGate,
    ) -> None:
        self._reranker = reranker
        self._context_packer = context_packer
        self._confidence_gate = confidence_gate

    async def process_documents(
        self,
        *,
        query: str,
        candidates: tuple[RetrievalCandidate, ...],
    ) -> PostRetrievalResult:
        return await self.process(query=query, candidates=document_evidence(candidates))

    async def process(
        self,
        *,
        query: str,
        candidates: tuple[EvidenceCandidate, ...],
    ) -> PostRetrievalResult:
        reranked = await self._reranker.rerank(query, candidates)
        deduplication = deduplicate(reranked)
        context = await self._context_packer.pack(deduplication.candidates)
        packed_evidence = tuple(source.evidence for source in context.sources)
        gate = self._confidence_gate.evaluate(packed_evidence)
        return PostRetrievalResult(
            reranked=reranked,
            deduplication=deduplication,
            context=context,
            gate=gate,
        )
