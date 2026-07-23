from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

MARKER_RE = re.compile(r"^\[S([1-9][0-9]*)\]$")
KNOWN_EVENTS = {"start", "status", "delta", "replace", "citations", "done", "error"}


class SSEProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SSEEvent:
    event: str
    data: dict[str, Any]
    event_id: str | None = None


@dataclass(slots=True)
class ChatAccumulator:
    answer: str = ""
    citations: dict[str, dict[str, Any]] = field(default_factory=dict)
    stages: list[str] = field(default_factory=list)
    assistant_message_id: str | None = None
    route: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    error_code: str | None = None
    retryable: bool = False
    started: bool = False
    done: bool = False

    def apply(self, event: SSEEvent) -> None:
        if event.event not in KNOWN_EVENTS:
            return
        data = event.data
        if event.event == "start":
            self.answer = ""
            self.citations = {}
            self.stages = []
            self.assistant_message_id = None
            self.route = None
            self.usage = {}
            self.error_code = None
            self.retryable = False
            self.started = True
            self.done = False
        elif event.event == "status":
            stage = data.get("stage")
            if isinstance(stage, str) and stage in {
                "planning",
                "retrieving",
                "reranking",
                "generating",
            }:
                self.stages.append(stage)
        elif event.event == "delta":
            text = data.get("text")
            if isinstance(text, str):
                self.answer += text
        elif event.event == "replace":
            text = data.get("text")
            if isinstance(text, str):
                self.answer = text
        elif event.event == "citations":
            self.citations = validate_citations(data.get("items"))
        elif event.event == "done":
            message_id = data.get("message_id")
            route = data.get("route")
            usage = data.get("usage")
            if isinstance(message_id, str):
                self.assistant_message_id = message_id
            if isinstance(route, str):
                self.route = route
            if isinstance(usage, dict):
                self.usage = {
                    key: value
                    for key, value in usage.items()
                    if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                }
            self.done = True
        elif event.event == "error":
            code = data.get("code")
            retryable = data.get("retryable")
            self.error_code = code if isinstance(code, str) else "chat_error"
            self.retryable = retryable if isinstance(retryable, bool) else False


def parse_sse(lines: Iterable[str]) -> Iterator[SSEEvent]:
    event_name = "message"
    event_id: str | None = None
    data_lines: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                yield _event(event_name, event_id, data_lines)
            event_name = "message"
            event_id = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field_name, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field_name == "event":
            event_name = value
        elif field_name == "id" and "\x00" not in value:
            event_id = value
        elif field_name == "data":
            data_lines.append(value)
    if data_lines:
        yield _event(event_name, event_id, data_lines)


def validate_citations(value: object) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    validated: dict[str, dict[str, Any]] = {}
    for marker, raw in value.items():
        if not isinstance(marker, str) or not isinstance(raw, dict):
            continue
        match = MARKER_RE.fullmatch(marker)
        if match is None:
            continue
        source_id = f"S{match.group(1)}"
        if raw.get("marker") != marker or raw.get("source_id") != source_id:
            continue
        source_type = raw.get("source_type")
        title = raw.get("title")
        if source_type not in {"document", "web"} or not isinstance(title, str) or not title:
            continue
        if source_type == "document":
            if not _uuid(raw.get("document_id")) or not _uuid(raw.get("document_version_id")):
                continue
        else:
            uri = raw.get("uri")
            if not isinstance(uri, str) or not _safe_http_url(uri):
                continue
        page_start = raw.get("page_start")
        page_end = raw.get("page_end")
        if page_start is not None and (
            not isinstance(page_start, int) or isinstance(page_start, bool) or page_start < 1
        ):
            continue
        if page_end is not None and (
            not isinstance(page_end, int) or isinstance(page_end, bool) or page_end < 1
        ):
            continue
        if isinstance(page_start, int) and isinstance(page_end, int) and page_end < page_start:
            continue
        validated[marker] = dict(raw)
    return validated


def _event(name: str, event_id: str | None, data_lines: list[str]) -> SSEEvent:
    try:
        payload = json.loads("\n".join(data_lines))
    except json.JSONDecodeError as exc:
        raise SSEProtocolError("SSE data is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise SSEProtocolError("SSE data must be a JSON object")
    return SSEEvent(event=name, data=payload, event_id=event_id)


def _uuid(value: object) -> bool:
    import uuid

    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _safe_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )
