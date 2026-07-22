from __future__ import annotations

import uuid
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.rag.retriever import RetrievalCandidate


class EvidenceCandidate(BaseModel):
    """Source-neutral evidence passed from retrieval into reranking."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1, max_length=512)
    source_type: Literal["document", "web"]
    source_key: str = Field(min_length=1, max_length=2048)
    section_key: str = Field(min_length=1, max_length=2048)
    domain: str | None = Field(default=None, max_length=253)
    title: str = Field(min_length=1, max_length=1024)
    source_filename: str | None = Field(default=None, max_length=1024)
    uri: str | None = Field(default=None, max_length=4096)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    text_original: str = Field(min_length=1)
    text_lexical: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lexical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retrieval_rank: int = Field(ge=1)
    retrieval_score: float
    matched_exact_terms: tuple[str, ...] = ()
    matched_hints: tuple[str, ...] = ()
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_source_fields(self) -> Self:
        if self.source_type == "web" and (self.domain is None or self.uri is None):
            raise ValueError("web evidence requires domain and uri")
        if (self.page_start is None) != (self.page_end is None):
            raise ValueError("page_start and page_end must be provided together")
        if self.page_start is not None and self.page_end is not None:
            if self.page_end < self.page_start:
                raise ValueError("page_end cannot precede page_start")
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("char_start and char_end must be provided together")
        if self.char_start is not None and self.char_end is not None:
            if self.char_end <= self.char_start:
                raise ValueError("char_end must follow char_start")
        return self

    @classmethod
    def from_retrieval(cls, candidate: RetrievalCandidate) -> Self:
        return cls(
            candidate_id=str(candidate.chunk_id),
            source_type="document",
            source_key=str(candidate.document_id),
            section_key=str(candidate.section_id),
            title=candidate.document_title,
            source_filename=candidate.source_filename,
            page_start=candidate.page_start,
            page_end=candidate.page_end,
            char_start=candidate.char_start,
            char_end=candidate.char_end,
            text_original=candidate.text_original,
            text_lexical=candidate.text_lexical,
            content_sha256=candidate.content_sha256,
            lexical_sha256=candidate.lexical_sha256,
            retrieval_rank=candidate.rank,
            retrieval_score=candidate.fusion_score,
            matched_exact_terms=candidate.matched_exact_terms,
            matched_hints=candidate.matched_hints,
            provenance={
                "tenant_id": str(candidate.tenant_id),
                "document_id": str(candidate.document_id),
                "document_version_id": str(candidate.document_version_id),
                "section_id": str(candidate.section_id),
                "section_path_original": candidate.section_path_original,
                "chunk_index": candidate.chunk_index,
                "dense_rank": candidate.dense_rank,
                "dense_score": candidate.dense_score,
                "sparse_rank": candidate.sparse_rank,
                "sparse_score": candidate.sparse_score,
                "exact_rank": candidate.exact_rank,
                "hint_rank": candidate.hint_rank,
                "is_neighbor": candidate.is_neighbor,
                "neighbor_of": (
                    str(candidate.neighbor_of) if candidate.neighbor_of is not None else None
                ),
            },
        )


class RerankedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate: EvidenceCandidate
    rerank_score: float = Field(ge=0.0, le=1.0)
    rerank_rank: int = Field(ge=1)


def document_evidence(candidates: tuple[RetrievalCandidate, ...]) -> tuple[EvidenceCandidate, ...]:
    return tuple(EvidenceCandidate.from_retrieval(candidate) for candidate in candidates)


def stable_candidate_key(candidate: EvidenceCandidate) -> tuple[int, str]:
    return candidate.retrieval_rank, candidate.candidate_id


def stable_reranked_key(item: RerankedEvidence) -> tuple[int, str]:
    return item.rerank_rank, item.candidate.candidate_id


def uuid_candidate_id(candidate: EvidenceCandidate) -> uuid.UUID | None:
    """Return the UUID when the source uses one; web IDs need not be UUIDs."""

    try:
        return uuid.UUID(candidate.candidate_id)
    except ValueError:
        return None
