from __future__ import annotations

import json
import uuid

import httpx
import pytest

import api_client
from api_client import ApiClient, ApiError, AuthenticationExpired, TokenBundle
from sse import ChatAccumulator


def _tokens() -> TokenBundle:
    return TokenBundle(
        access_token="old-access",  # noqa: S106 - inert test token
        refresh_token="old-refresh-token-that-is-long-enough",  # noqa: S106
        tenant_id=str(uuid.uuid4()),
        access_expires_in=900,
        refresh_expires_in=3600,
    )


def _token_response(tokens: TokenBundle) -> dict[str, object]:
    return {
        "access_token": "new-access",
        "refresh_token": "new-refresh-token-that-is-long-enough",
        "tenant_id": tokens.tenant_id,
        "expires_in": 900,
        "refresh_expires_in": 3600,
    }


def test_401_refreshes_once_and_rotates_server_side_tokens() -> None:
    tokens = _tokens()
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/auth/refresh":
            assert json.loads(request.content)["refresh_token"] == (
                "old-refresh-token-that-is-long-enough"  # noqa: S105
            )
            return httpx.Response(200, json=_token_response(tokens))
        if request.headers.get("authorization") == "Bearer old-access":
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(
            200,
            json={
                "user_id": str(uuid.uuid4()),
                "email": "member@example.test",
                "active_tenant_id": tokens.tenant_id,
                "is_superuser": False,
                "memberships": [],
            },
        )

    client = ApiClient(tokens, transport=httpx.MockTransport(handler))
    result = client.get_me()

    assert result["email"] == "member@example.test"
    assert tokens.access_token == "new-access"  # noqa: S105
    assert tokens.refresh_token == (
        "new-refresh-token-that-is-long-enough"  # noqa: S105
    )
    assert calls == [
        ("GET", "/api/me"),
        ("POST", "/api/auth/refresh"),
        ("GET", "/api/me"),
    ]
    assert "old-access" not in repr(tokens)


def test_second_401_after_refresh_expires_session() -> None:
    tokens = _tokens()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/refresh":
            return httpx.Response(200, json=_token_response(tokens))
        return httpx.Response(401, json={"detail": "disabled"})

    client = ApiClient(tokens, transport=httpx.MockTransport(handler))
    with pytest.raises(AuthenticationExpired):
        client.get_me()


def test_stream_reconnect_reuses_exact_request_and_replay_resets_partial_text() -> None:
    tokens = _tokens()
    chat_requests: list[tuple[dict[str, object], str]] = []
    document_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    assistant_id = str(uuid.uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        body = json.loads(request.content)
        chat_requests.append((body, request.headers["idempotency-key"]))
        if len(chat_requests) == 1:
            stream = (
                'event: start\ndata: {}\n\nevent: delta\ndata: {"text":"partial duplicate"}\n\n'
            )
        else:
            citations = {
                "[S1]": {
                    "marker": "[S1]",
                    "source_id": "S1",
                    "source_type": "document",
                    "title": "Guide",
                    "document_id": document_id,
                    "document_version_id": version_id,
                    "page_start": 1,
                }
            }
            stream = (
                "event: start\ndata: {}\n\n"
                'event: delta\ndata: {"text":"unsafe draft [S9]"}\n\n'
                'event: replace\ndata: {"text":"safe answer [S1]"}\n\n'
                f"event: citations\ndata: {json.dumps({'items': citations})}\n\n"
                "event: done\n"
                f"data: {json.dumps({'message_id': assistant_id, 'route': 'rag'})}\n\n"
            )
        return httpx.Response(200, text=stream)

    client = ApiClient(tokens, transport=httpx.MockTransport(handler))
    accumulator = ChatAccumulator()
    for event in client.stream_chat(
        session_id=str(uuid.uuid4()),
        message="Question",
        collection_ids=[],
        document_ids=[document_id],
        web_search=True,
        client_request_id=str(uuid.uuid4()),
        idempotency_key="chat-stable-key",
    ):
        accumulator.apply(event)

    assert len(chat_requests) == 2
    assert chat_requests[0] == chat_requests[1]
    assert accumulator.answer == "safe answer [S1]"
    assert "[S9]" not in accumulator.answer
    assert accumulator.done is True
    assert set(accumulator.citations) == {"[S1]"}


def test_public_file_url_accepts_only_caddy_file_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_client, "PUBLIC_BASE_URL", "https://ragbox.local")
    assert (
        api_client.public_file_url("/api/files/opaque-token#page=2")
        == "https://ragbox.local/api/files/opaque-token#page=2"
    )
    for unsafe in (
        "https://attacker.example/api/files/token",
        "//attacker.example/api/files/token",
        "/api/documents/private",
    ):
        with pytest.raises(ApiError):
            api_client.public_file_url(unsafe)
