from __future__ import annotations

import codecs
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docx import Document as DocxDocument
from pypdf import PdfReader

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
JSONL_MIMES = frozenset({"application/x-ndjson", "application/jsonl"})
MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
DOCX_HEADING_RE = re.compile(r"^Heading ([1-6])$")


class ParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    page_number: int
    text_original: str
    heading_path: tuple[str, ...]
    source_metadata: dict[str, Any]


def parse_document(path: Path, mime: str, *, expanded_limit_bytes: int) -> list[ParsedBlock]:
    if mime in JSONL_MIMES:
        blocks = _parse_jsonl(path)
    elif mime == "application/pdf":
        blocks = _parse_pdf(path)
    elif mime == DOCX_MIME:
        _validate_docx_archive(path, expanded_limit_bytes)
        blocks = _parse_docx(path)
    elif mime == "text/markdown":
        blocks = _parse_markdown(path.read_text(encoding="utf-8-sig"))
    elif mime == "text/plain":
        blocks = _parse_text(path.read_text(encoding="utf-8-sig"))
    else:
        raise ParseError("unsupported MIME type")
    if not blocks or not any(block.text_original.strip() for block in blocks):
        raise ParseError("document contains no extractable text")
    return blocks


def _parse_jsonl(path: Path) -> list[ParsedBlock]:
    blocks: list[ParsedBlock] = []
    with path.open("r", encoding="utf-8-sig") as source:
        for line_number, raw_line in enumerate(source, start=1):
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ParseError(f"invalid JSON on line {line_number}") from exc
            if not isinstance(row, dict):
                raise ParseError(f"line {line_number} must be a JSON object")
            text = row.get("text")
            source_page = row.get("source_page")
            category = row.get("category")
            word_count = row.get("word_count")
            if not isinstance(text, str) or not text.strip():
                raise ParseError(f"line {line_number} has invalid text")
            if not isinstance(source_page, str | int) or not str(source_page).strip():
                raise ParseError(f"line {line_number} has invalid source_page")
            if not isinstance(category, str) or not category.strip():
                raise ParseError(f"line {line_number} has invalid category")
            if not isinstance(word_count, int) or isinstance(word_count, bool) or word_count < 0:
                raise ParseError(f"line {line_number} has invalid word_count")
            blocks.append(
                ParsedBlock(
                    page_number=line_number,
                    text_original=text,
                    heading_path=(category.strip(), str(source_page).strip()),
                    source_metadata={
                        "source_page": source_page,
                        "category": category,
                        "word_count": word_count,
                        "jsonl_line": line_number,
                    },
                )
            )
    return blocks


def _parse_pdf(path: Path) -> list[ParsedBlock]:
    try:
        reader = PdfReader(path, strict=True)
        return [
            ParsedBlock(
                page_number=number,
                text_original=page.extract_text() or "",
                heading_path=(f"Page {number}",),
                source_metadata={"source_page": number},
            )
            for number, page in enumerate(reader.pages, start=1)
        ]
    except Exception as exc:
        raise ParseError("invalid or unsupported PDF") from exc


def _parse_docx(path: Path) -> list[ParsedBlock]:
    try:
        document = DocxDocument(str(path))
    except Exception as exc:
        raise ParseError("invalid DOCX") from exc
    hierarchy: list[str] = []
    blocks: list[ParsedBlock] = []
    page = 1
    for paragraph in document.paragraphs:
        text = paragraph.text
        if not text.strip():
            continue
        style_name = paragraph.style.name if paragraph.style is not None else ""
        match = DOCX_HEADING_RE.fullmatch(style_name)
        if match:
            level = int(match.group(1))
            hierarchy = hierarchy[: level - 1]
            hierarchy.append(text.strip())
            continue
        blocks.append(
            ParsedBlock(
                page_number=page,
                text_original=text,
                heading_path=tuple(hierarchy) or ("Document",),
                source_metadata={"source_page": page},
            )
        )
    return blocks


def _parse_markdown(text: str) -> list[ParsedBlock]:
    hierarchy: list[str] = []
    current_lines: list[str] = []
    blocks: list[ParsedBlock] = []

    def flush() -> None:
        if current_lines and any(line.strip() for line in current_lines):
            blocks.append(
                ParsedBlock(
                    page_number=1,
                    text_original="\n".join(current_lines),
                    heading_path=tuple(hierarchy) or ("Document",),
                    source_metadata={"source_page": 1},
                )
            )
        current_lines.clear()

    for line in text.splitlines():
        match = MARKDOWN_HEADING_RE.match(line)
        if match:
            flush()
            level = len(match.group(1))
            hierarchy = hierarchy[: level - 1]
            hierarchy.append(match.group(2).strip())
        else:
            current_lines.append(line)
    flush()
    return blocks


def _parse_text(text: str) -> list[ParsedBlock]:
    return [
        ParsedBlock(
            page_number=number,
            text_original=page,
            heading_path=(f"Page {number}",),
            source_metadata={"source_page": number},
        )
        for number, page in enumerate(text.split("\f"), start=1)
    ]


def _validate_docx_archive(path: Path, expanded_limit_bytes: int) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = {entry.filename for entry in archive.infolist()}
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise ParseError("file is not a DOCX package")
            expanded_size = 0
            for entry in archive.infolist():
                pure = Path(entry.filename)
                if pure.is_absolute() or ".." in pure.parts:
                    raise ParseError("unsafe DOCX archive path")
                expanded_size += entry.file_size
                if expanded_size > expanded_limit_bytes:
                    raise ParseError("expanded DOCX exceeds the upload limit")
    except zipfile.BadZipFile as exc:
        raise ParseError("invalid DOCX archive") from exc


def sniff_mime(filename: str, declared_mime: str | None, prefix: bytes) -> str:
    suffix = Path(filename).suffix.casefold()
    by_extension = {
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".jsonl": "application/x-ndjson",
        ".ndjson": "application/x-ndjson",
        ".docx": DOCX_MIME,
    }
    expected = by_extension.get(suffix)
    if expected is None:
        raise ParseError("unsupported file extension")
    if declared_mime and declared_mime not in {
        expected,
        "application/octet-stream",
        "application/json" if expected in JSONL_MIMES else expected,
    }:
        raise ParseError("declared MIME does not match the extension")
    if expected == "application/pdf" and not prefix.startswith(b"%PDF-"):
        raise ParseError("PDF magic bytes are missing")
    if expected == DOCX_MIME and not prefix.startswith(b"PK\x03\x04"):
        raise ParseError("DOCX magic bytes are missing")
    if expected in {"text/plain", "text/markdown", "application/x-ndjson"}:
        try:
            decoder = codecs.getincrementaldecoder("utf-8-sig")()
            decoder.decode(prefix, final=False)
        except UnicodeDecodeError as exc:
            raise ParseError("text uploads must be UTF-8") from exc
    return expected
