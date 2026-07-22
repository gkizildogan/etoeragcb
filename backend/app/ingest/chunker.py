from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from app.ingest.hashing import sha256_text, stable_chunk_id
from app.ingest.normalization import normalize_lexical
from app.ingest.sections import SectionedBlock


class ChunkingError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TokenSpan:
    start: int
    end: int


class Tokenizer(Protocol):
    async def token_spans(self, text: str) -> list[TokenSpan]: ...


@dataclass(frozen=True, slots=True)
class BuiltChunk:
    id: uuid.UUID
    section_id: uuid.UUID
    occurrence_index: int
    chunk_index: int
    page_start: int
    page_end: int
    char_start: int
    char_end: int
    content_sha256: str
    lexical_sha256: str
    token_count: int
    text_original: str
    text_lexical: str


async def chunk_blocks(
    document_version_id: uuid.UUID,
    blocks: list[SectionedBlock],
    tokenizer: Tokenizer,
    *,
    max_tokens: int,
    overlap: int,
) -> list[BuiltChunk]:
    if overlap >= max_tokens:
        raise ChunkingError("overlap must be smaller than the chunk size")
    occurrence_by_hash: Counter[str] = Counter()
    result: list[BuiltChunk] = []
    step = max_tokens - overlap
    for sectioned in blocks:
        source_text = sectioned.block.text_original
        if not source_text.strip():
            continue
        spans = await tokenizer.token_spans(source_text)
        spans = [span for span in spans if 0 <= span.start < span.end <= len(source_text)]
        if not spans:
            raise ChunkingError("serving tokenizer returned no source token offsets")
        for token_start in range(0, len(spans), step):
            token_end = min(token_start + max_tokens, len(spans))
            window = spans[token_start:token_end]
            char_start = window[0].start
            char_end = window[-1].end
            original = source_text[char_start:char_end]
            lexical = normalize_lexical(original)
            content_hash = sha256_text(original)
            lexical_hash = sha256_text(lexical)
            occurrence = occurrence_by_hash[lexical_hash]
            occurrence_by_hash[lexical_hash] += 1
            chunk_index = len(result)
            result.append(
                BuiltChunk(
                    id=stable_chunk_id(
                        document_version_id=document_version_id,
                        page_start=sectioned.block.page_number,
                        section_id=sectioned.section_id,
                        char_start=char_start,
                        occurrence_index=occurrence,
                        content_sha256=content_hash,
                    ),
                    section_id=sectioned.section_id,
                    occurrence_index=occurrence,
                    chunk_index=chunk_index,
                    page_start=sectioned.block.page_number,
                    page_end=sectioned.block.page_number,
                    char_start=char_start,
                    char_end=char_end,
                    content_sha256=content_hash,
                    lexical_sha256=lexical_hash,
                    token_count=len(window),
                    text_original=original,
                    text_lexical=lexical,
                )
            )
            if token_end == len(spans):
                break
    if not result:
        raise ChunkingError("document produced no chunks")
    return result
