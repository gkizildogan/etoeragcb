from __future__ import annotations

import re
from dataclasses import dataclass

from app.chat.schemas import Citation
from app.rag.context import PackedSource

MARKER_RE = re.compile(r"\[S([1-9][0-9]*)\]")
ANY_MARKER_RE = re.compile(r"\[S([0-9]+)\]")
INCOMPLETE_MARKER_RE = re.compile(r"\[S[0-9]*$")
ADJACENT_DUPLICATE_RE = re.compile(r"(\[S[1-9][0-9]*\])(?:\s*\1)+")


@dataclass(frozen=True, slots=True)
class SanitizedAnswer:
    text: str
    cited_source_ids: tuple[str, ...]
    repaired: bool


class CitationStreamSanitizer:
    """Emits only complete, allow-listed citation markers."""

    def __init__(self, allowed_source_ids: set[str]) -> None:
        self._allowed = allowed_source_ids
        self._pending = ""
        self._raw_parts: list[str] = []
        self._emitted_parts: list[str] = []
        self._repaired = False

    def feed(self, fragment: str) -> str:
        if not fragment:
            return ""
        self._raw_parts.append(fragment)
        self._pending += fragment
        emitted = self._drain(final=False)
        if emitted:
            self._emitted_parts.append(emitted)
        return emitted

    def finish(self) -> SanitizedAnswer:
        emitted = self._drain(final=True)
        if emitted:
            self._emitted_parts.append(emitted)
        streamed = "".join(self._emitted_parts)
        authoritative = _repair_complete_answer("".join(self._raw_parts), self._allowed)
        repaired = self._repaired or authoritative != streamed
        cited = _ordered_source_ids(authoritative)
        return SanitizedAnswer(
            text=authoritative,
            cited_source_ids=cited,
            repaired=repaired,
        )

    @property
    def streamed_text(self) -> str:
        return "".join(self._emitted_parts)

    def _drain(self, *, final: bool) -> str:
        output: list[str] = []
        cursor = 0
        value = self._pending
        while cursor < len(value):
            opening = value.find("[", cursor)
            if opening < 0:
                output.append(value[cursor:])
                cursor = len(value)
                break
            output.append(value[cursor:opening])
            suffix = value[opening:]
            marker = ANY_MARKER_RE.match(suffix)
            if marker is not None:
                token = marker.group(0)
                source_id = f"S{marker.group(1)}"
                if source_id in self._allowed:
                    output.append(token)
                else:
                    self._repaired = True
                cursor = opening + len(token)
                continue
            if not final and (
                suffix == "[" or suffix == "[S" or INCOMPLETE_MARKER_RE.fullmatch(suffix)
            ):
                cursor = opening
                break
            if final and (
                suffix == "[S" or INCOMPLETE_MARKER_RE.fullmatch(suffix)
            ):
                self._repaired = True
                cursor = len(value)
                break
            output.append("[")
            cursor = opening + 1
        self._pending = value[cursor:]
        if final and self._pending:
            output.append(self._pending)
            self._pending = ""
        return "".join(output)


def citations_for_answer(
    answer: SanitizedAnswer,
    sources: tuple[PackedSource, ...],
) -> dict[str, Citation]:
    by_id = {source.source_id: source for source in sources}
    result: dict[str, Citation] = {}
    for source_id in answer.cited_source_ids:
        source = by_id.get(source_id)
        if source is None:
            continue
        candidate = source.evidence.candidate
        provenance = candidate.provenance
        document_id = _uuid_or_none(provenance.get("document_id"))
        document_version_id = _uuid_or_none(provenance.get("document_version_id"))
        marker = f"[{source_id}]"
        result[marker] = Citation(
            marker=marker,
            source_id=source_id,
            source_type=candidate.source_type,
            title=candidate.title,
            document_id=document_id,
            document_version_id=document_version_id,
            source_filename=candidate.source_filename,
            page_start=candidate.page_start,
            page_end=candidate.page_end,
            uri=candidate.uri,
        )
    return result


def _repair_complete_answer(value: str, allowed_source_ids: set[str]) -> str:
    repaired = ANY_MARKER_RE.sub(
        lambda match: match.group(0)
        if f"S{match.group(1)}" in allowed_source_ids
        else "",
        value,
    )
    repaired = INCOMPLETE_MARKER_RE.sub("", repaired)
    repaired = ADJACENT_DUPLICATE_RE.sub(r"\1", repaired)
    repaired = re.sub(r"[ \t]+([,.;:!?])", r"\1", repaired)
    repaired = re.sub(r"[ \t]{2,}", " ", repaired)
    return repaired.strip()


def _ordered_source_ids(value: str) -> tuple[str, ...]:
    found: list[str] = []
    seen: set[str] = set()
    for match in MARKER_RE.finditer(value):
        source_id = f"S{match.group(1)}"
        if source_id not in seen:
            found.append(source_id)
            seen.add(source_id)
    return tuple(found)


def _uuid_or_none(value: object):  # type: ignore[no-untyped-def]
    import uuid

    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None
