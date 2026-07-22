from __future__ import annotations

import uuid
from dataclasses import dataclass, replace

from app.ingest.normalization import normalize_lexical
from app.ingest.parsers import ParsedBlock


@dataclass(frozen=True, slots=True)
class BuiltSection:
    id: uuid.UUID
    parent_id: uuid.UUID | None
    ordinal: int
    level: int
    heading_original: str
    heading_lexical: str
    page_start: int
    page_end: int
    path_original: str
    path_lexical: str
    source_metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class SectionedBlock:
    block: ParsedBlock
    section_id: uuid.UUID


def build_sections(
    document_version_id: uuid.UUID, blocks: list[ParsedBlock]
) -> tuple[list[BuiltSection], list[SectionedBlock]]:
    sections_by_path: dict[tuple[str, ...], BuiltSection] = {}
    sectioned: list[SectionedBlock] = []
    ordinal = 0
    for block in blocks:
        raw_path = tuple(part.strip() for part in block.heading_path if part.strip()) or (
            "Document",
        )
        parent_id: uuid.UUID | None = None
        for depth in range(1, len(raw_path) + 1):
            path = raw_path[:depth]
            existing = sections_by_path.get(path)
            if existing is None:
                path_lexical_parts = tuple(normalize_lexical(part) for part in path)
                existing = BuiltSection(
                    id=uuid.uuid5(document_version_id, f"section:{'/'.join(path_lexical_parts)}"),
                    parent_id=parent_id,
                    ordinal=ordinal,
                    level=depth,
                    heading_original=path[-1],
                    heading_lexical=path_lexical_parts[-1],
                    page_start=block.page_number,
                    page_end=block.page_number,
                    path_original=" / ".join(path),
                    path_lexical=" / ".join(path_lexical_parts),
                    source_metadata=(dict(block.source_metadata) if depth == len(raw_path) else {}),
                )
                sections_by_path[path] = existing
                ordinal += 1
            elif block.page_number > existing.page_end:
                existing = replace(existing, page_end=block.page_number)
                sections_by_path[path] = existing
            parent_id = existing.id
        assert parent_id is not None
        sectioned.append(SectionedBlock(block=block, section_id=parent_id))
    return sorted(sections_by_path.values(), key=lambda item: item.ordinal), sectioned
