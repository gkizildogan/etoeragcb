from __future__ import annotations

import json
import uuid

import pytest

from sse import ChatAccumulator, SSEEvent, SSEProtocolError, parse_sse, validate_citations


def test_fragmented_multiline_events_and_replace_are_authoritative() -> None:
    events = list(
        parse_sse(
            [
                ": keepalive",
                "id: 1",
                "event: start",
                'data: {"request_id":"r1"}',
                "",
                "event: delta",
                'data: {"text":"draft"}',
                "",
                "event: replace",
                "data: {",
                'data: "text":"safe final"}',
                "",
                "event: done",
                'data: {"message_id":"m1","route":"rag","usage":{"total_tokens":7}}',
            ]
        )
    )
    accumulator = ChatAccumulator()
    for event in events:
        accumulator.apply(event)

    assert [event.event for event in events] == ["start", "delta", "replace", "done"]
    assert events[0].event_id == "1"
    assert accumulator.answer == "safe final"
    assert accumulator.done is True
    assert accumulator.usage == {"total_tokens": 7}


def test_replayed_start_resets_partial_stream_and_unknown_events_are_tolerated() -> None:
    accumulator = ChatAccumulator()
    accumulator.apply(SSEEvent("start", {}))
    accumulator.apply(SSEEvent("delta", {"text": "duplicated"}))
    accumulator.apply(SSEEvent("future_event", {"text": "ignored"}))
    accumulator.apply(SSEEvent("start", {}))
    accumulator.apply(SSEEvent("delta", {"text": "once"}))

    assert accumulator.answer == "once"
    assert accumulator.started is True


def test_invalid_json_is_rejected() -> None:
    with pytest.raises(SSEProtocolError):
        list(parse_sse(["event: delta", "data: not-json", ""]))


def test_only_safe_well_formed_citations_survive() -> None:
    document_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    raw = {
        "[S1]": {
            "marker": "[S1]",
            "source_id": "S1",
            "source_type": "document",
            "title": "Handbook",
            "document_id": document_id,
            "document_version_id": version_id,
            "page_start": 2,
            "page_end": 3,
        },
        "[S2]": {
            "marker": "[S2]",
            "source_id": "S2",
            "source_type": "web",
            "title": "Safe page",
            "uri": "https://example.org/article",
        },
        "[S3]": {
            "marker": "[S3]",
            "source_id": "S3",
            "source_type": "web",
            "title": "Credential URL",
            "uri": "https://user:password@example.org/",
        },
        "[S4]": {
            "marker": "[S4]",
            "source_id": "S4",
            "source_type": "document",
            "title": "Backwards pages",
            "document_id": document_id,
            "document_version_id": version_id,
            "page_start": 4,
            "page_end": 2,
        },
        "<script>": {
            "marker": "<script>",
            "source_id": "S5",
            "source_type": "web",
            "title": "Bad marker",
            "uri": "https://example.org/",
        },
    }

    assert validate_citations(raw) == {
        "[S1]": raw["[S1]"],
        "[S2]": raw["[S2]"],
    }


def test_sse_requires_json_objects() -> None:
    value = json.dumps(["not", "an", "object"])
    with pytest.raises(SSEProtocolError):
        list(parse_sse(["event: delta", f"data: {value}", ""]))
