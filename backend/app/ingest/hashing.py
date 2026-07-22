from __future__ import annotations

import hashlib
import math
import re
import uuid
from collections import Counter
from dataclasses import dataclass

LEXEME_RE = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)?", re.UNICODE)
CHUNK_NAMESPACE = uuid.UUID("d3df9412-c3e2-58ad-9c4d-f82982ca5a50")
SPARSE_BUCKETS = 2**31 - 1


@dataclass(frozen=True, slots=True)
class SparseVector:
    indices: list[int]
    values: list[float]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_chunk_id(
    *,
    document_version_id: uuid.UUID,
    page_start: int,
    section_id: uuid.UUID | None,
    char_start: int,
    occurrence_index: int,
    content_sha256: str,
) -> uuid.UUID:
    identity = (
        f"{document_version_id}:{page_start}:{section_id or 'none'}:{char_start}:"
        f"{occurrence_index}:{content_sha256}"
    )
    return uuid.uuid5(CHUNK_NAMESPACE, identity)


def sparse_lexical_vector(text_lexical: str) -> SparseVector:
    counts = Counter(LEXEME_RE.findall(text_lexical))
    buckets: dict[int, float] = {}
    for term, frequency in counts.items():
        bucket = int.from_bytes(hashlib.sha256(term.encode("utf-8")).digest()[:8], "big")
        bucket %= SPARSE_BUCKETS
        buckets[bucket] = buckets.get(bucket, 0.0) + 1.0 + math.log(frequency)
    norm = math.sqrt(sum(value * value for value in buckets.values())) or 1.0
    ordered = sorted(buckets.items())
    return SparseVector(
        indices=[index for index, _ in ordered],
        values=[value / norm for _, value in ordered],
    )
