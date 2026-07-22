from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.rag.candidates import RerankedEvidence, stable_reranked_key


class DuplicateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dropped_candidate_id: str
    kept_candidate_id: str
    reason: Literal["content_hash", "lexical_hash", "overlapping_span", "near_duplicate"]
    similarity: float = Field(ge=0.0, le=1.0)


class DeduplicationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidates: tuple[RerankedEvidence, ...]
    decisions: tuple[DuplicateDecision, ...]


def deduplicate(
    candidates: tuple[RerankedEvidence, ...],
    *,
    overlap_threshold: float = 0.8,
    near_duplicate_threshold: float = 0.85,
) -> DeduplicationResult:
    """Collapse repeated evidence while retaining exact-term provenance."""

    if not 0.0 <= overlap_threshold <= 1.0:
        raise ValueError("overlap_threshold must be between zero and one")
    if not 0.0 <= near_duplicate_threshold <= 1.0:
        raise ValueError("near_duplicate_threshold must be between zero and one")
    kept: list[RerankedEvidence] = []
    decisions: list[DuplicateDecision] = []
    for current in sorted(candidates, key=stable_reranked_key):
        duplicate: (
            tuple[
                int,
                Literal["content_hash", "lexical_hash", "overlapping_span", "near_duplicate"],
                float,
            ]
            | None
        ) = None
        for index, existing in enumerate(kept):
            reason = _duplicate_reason(
                existing,
                current,
                overlap_threshold=overlap_threshold,
                near_duplicate_threshold=near_duplicate_threshold,
            )
            if reason is not None:
                duplicate = (index, reason[0], reason[1])
                break
        if duplicate is None:
            kept.append(current)
            continue
        index, duplicate_reason, similarity = duplicate
        survivor = kept[index]
        kept[index] = _merge_exact_provenance(survivor, current)
        decisions.append(
            DuplicateDecision(
                dropped_candidate_id=current.candidate.candidate_id,
                kept_candidate_id=survivor.candidate.candidate_id,
                reason=duplicate_reason,
                similarity=similarity,
            )
        )
    return DeduplicationResult(candidates=tuple(kept), decisions=tuple(decisions))


def _duplicate_reason(
    existing: RerankedEvidence,
    current: RerankedEvidence,
    *,
    overlap_threshold: float,
    near_duplicate_threshold: float,
) -> (
    tuple[Literal["content_hash", "lexical_hash", "overlapping_span", "near_duplicate"], float]
    | None
):
    left = existing.candidate
    right = current.candidate
    if left.content_sha256 == right.content_sha256:
        return "content_hash", 1.0
    if left.lexical_sha256 == right.lexical_sha256:
        return "lexical_hash", 1.0

    unique_exact_terms = set(right.matched_exact_terms) - set(left.matched_exact_terms)
    if unique_exact_terms:
        return None
    overlap = _span_overlap(existing, current)
    if overlap >= overlap_threshold:
        return "overlapping_span", overlap
    similarity = _shingle_similarity(left.text_lexical, right.text_lexical)
    if similarity >= near_duplicate_threshold:
        return "near_duplicate", similarity
    return None


def _span_overlap(left: RerankedEvidence, right: RerankedEvidence) -> float:
    first = left.candidate
    second = right.candidate
    if (
        first.source_key != second.source_key
        or first.section_key != second.section_key
        or first.char_start is None
        or first.char_end is None
        or second.char_start is None
        or second.char_end is None
    ):
        return 0.0
    intersection = max(
        0, min(first.char_end, second.char_end) - max(first.char_start, second.char_start)
    )
    shortest = min(first.char_end - first.char_start, second.char_end - second.char_start)
    return intersection / shortest if shortest > 0 else 0.0


def _shingle_similarity(left: str, right: str) -> float:
    left_shingles = _shingles(left)
    right_shingles = _shingles(right)
    if not left_shingles or not right_shingles:
        return 0.0
    return len(left_shingles & right_shingles) / len(left_shingles | right_shingles)


def _shingles(text: str) -> set[tuple[str, ...]]:
    tokens = text.split()
    if len(tokens) < 3:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[index : index + 3]) for index in range(len(tokens) - 2)}


def _merge_exact_provenance(
    survivor: RerankedEvidence, duplicate: RerankedEvidence
) -> RerankedEvidence:
    left = survivor.candidate
    right = duplicate.candidate
    exact_terms = tuple(dict.fromkeys((*left.matched_exact_terms, *right.matched_exact_terms)))
    hints = tuple(dict.fromkeys((*left.matched_hints, *right.matched_hints)))
    provenance = dict(left.provenance)
    collapsed = provenance.get("collapsed_candidate_ids", [])
    collapsed_ids = [str(value) for value in collapsed] if isinstance(collapsed, list) else []
    collapsed_ids.append(right.candidate_id)
    provenance["collapsed_candidate_ids"] = collapsed_ids
    candidate = left.model_copy(
        update={
            "matched_exact_terms": exact_terms,
            "matched_hints": hints,
            "provenance": provenance,
        }
    )
    return survivor.model_copy(update={"candidate": candidate})
