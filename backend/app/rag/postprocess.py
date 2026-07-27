from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.rag.candidates import EvidenceCandidate, RerankedEvidence, document_evidence
from app.rag.confidence import CandidateConfidenceFilter, CandidateConfidenceResult
from app.rag.context import ContextPacker, PackedContext
from app.rag.dedup import DeduplicationResult, deduplicate
from app.rag.gate import ConfidenceGate, GateDecision
from app.rag.reranker import Reranker
from app.rag.retriever import RetrievalCandidate


class PostRetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reranked: tuple[RerankedEvidence, ...]
    deduplication: DeduplicationResult
    confidence_filter: CandidateConfidenceResult
    context: PackedContext
    gate: GateDecision


class PostRetrievalService:
    def __init__(
        self,
        reranker: Reranker,
        context_packer: ContextPacker,
        confidence_gate: ConfidenceGate,
        confidence_filter: CandidateConfidenceFilter,
    ) -> None:
        self._reranker = reranker
        self._context_packer = context_packer
        self._confidence_gate = confidence_gate
        self._confidence_filter = confidence_filter

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
        confidence_filter = self._confidence_filter.filter(deduplication.candidates)
        context = await self._context_packer.pack(confidence_filter.candidates)
        packed_evidence = tuple(source.evidence for source in context.sources)
        gate = self._confidence_gate.evaluate(packed_evidence)
        return PostRetrievalResult(
            reranked=reranked,
            deduplication=deduplication,
            confidence_filter=confidence_filter,
            context=context,
            gate=gate,
        )
