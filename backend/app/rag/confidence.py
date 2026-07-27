from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.rag.candidates import RerankedEvidence, stable_reranked_key


class CandidateConfidenceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    rerank_rank: int = Field(ge=1)
    rerank_score: float = Field(ge=0.0, le=1.0)
    reason: Literal["below_score_min", "outside_top_delta"]


class CandidateConfidenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidates: tuple[RerankedEvidence, ...]
    decisions: tuple[CandidateConfidenceDecision, ...]
    top_score: float | None = Field(default=None, ge=0.0, le=1.0)
    effective_score_cutoff: float = Field(ge=0.0, le=1.0)
    score_min: float = Field(ge=0.0, le=1.0)
    top_delta: float = Field(ge=0.0, le=1.0)


class CandidateConfidenceFilter:
    """Remove weak reranked evidence before context diversity and packing."""

    def __init__(self, *, score_min: float, top_delta: float) -> None:
        if not 0.0 <= score_min <= 1.0:
            raise ValueError("candidate confidence score minimum must be within [0, 1]")
        if not 0.0 <= top_delta <= 1.0:
            raise ValueError("candidate confidence top delta must be within [0, 1]")
        self._score_min = score_min
        self._top_delta = top_delta

    def filter(self, candidates: tuple[RerankedEvidence, ...]) -> CandidateConfidenceResult:
        ranked = tuple(sorted(candidates, key=stable_reranked_key))
        top_score = ranked[0].rerank_score if ranked else None
        effective_cutoff = (
            max(self._score_min, top_score - self._top_delta)
            if top_score is not None
            else self._score_min
        )
        retained: list[RerankedEvidence] = []
        decisions: list[CandidateConfidenceDecision] = []
        for item in ranked:
            if item.rerank_score >= effective_cutoff:
                retained.append(item)
                continue
            reason: Literal["below_score_min", "outside_top_delta"] = (
                "below_score_min" if item.rerank_score < self._score_min else "outside_top_delta"
            )
            decisions.append(
                CandidateConfidenceDecision(
                    candidate_id=item.candidate.candidate_id,
                    rerank_rank=item.rerank_rank,
                    rerank_score=item.rerank_score,
                    reason=reason,
                )
            )
        return CandidateConfidenceResult(
            candidates=tuple(retained),
            decisions=tuple(decisions),
            top_score=top_score,
            effective_score_cutoff=effective_cutoff,
            score_min=self._score_min,
            top_delta=self._top_delta,
        )
