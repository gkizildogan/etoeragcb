from __future__ import annotations

import codecs
import re
import unicodedata
from html.parser import HTMLParser

from app.web.security import WebFetchError

ALLOWED_MEDIA_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
ALLOWED_ENCODINGS = {"utf-8", "iso8859-1", "cp1252"}
BLOCK_TAGS = {
    "article",
    "aside",
    "blockquote",
    "br",
    "div",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "li",
    "main",
    "nav",
    "p",
    "section",
    "table",
    "td",
    "th",
    "tr",
}
SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "object"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.lower()
        if normalized in SKIP_TAGS:
            self._skip_depth += 1
        elif self._skip_depth == 0 and normalized in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif self._skip_depth == 0 and normalized in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.parts.append(data)


def extract_text(body: bytes, content_type: str | None, max_chars: int) -> tuple[str, str]:
    media_type, encoding = _parse_content_type(content_type)
    try:
        decoded = body.decode(encoding, errors="replace")
    except LookupError as exc:
        raise WebFetchError("unsupported_content_type", "unsupported response charset") from exc
    if media_type == "text/plain":
        raw_text = decoded
    else:
        parser = _TextExtractor()
        try:
            parser.feed(decoded)
            parser.close()
        except (ValueError, AssertionError) as exc:
            raise WebFetchError("invalid_content", "HTML extraction failed") from exc
        raw_text = "".join(parser.parts)
    sanitized = sanitize_text(raw_text)
    if not sanitized:
        raise WebFetchError("empty_content", "page contained no usable text")
    return sanitized[:max_chars], media_type


def _parse_content_type(value: str | None) -> tuple[str, str]:
    if value is None:
        raise WebFetchError("unsupported_content_type", "Content-Type is required")
    pieces = [piece.strip() for piece in value.split(";")]
    media_type = pieces[0].lower()
    if media_type not in ALLOWED_MEDIA_TYPES:
        raise WebFetchError("unsupported_content_type", "response media type is not allowed")
    encoding = "utf-8"
    for piece in pieces[1:]:
        if piece.lower().startswith("charset="):
            encoding = piece.split("=", 1)[1].strip(" \"'").lower()
            break
    try:
        canonical = codecs.lookup(encoding).name
    except LookupError as exc:
        raise WebFetchError("unsupported_content_type", "response charset is unknown") from exc
    if canonical not in ALLOWED_ENCODINGS:
        raise WebFetchError("unsupported_content_type", "response charset is not allowed")
    return media_type, canonical


def sanitize_text(text: str) -> str:
    cleaned = "".join(
        character
        for character in text
        if character in {"\n", "\t"} or not unicodedata.category(character).startswith("C")
    )
    cleaned = cleaned.replace("\r", "\n")
    cleaned = re.sub(r"[\t ]+", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()
