from __future__ import annotations

import uuid
from typing import Any, ClassVar
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from api_client import TokenBundle
from sse import SSEEvent

TENANT_ID = str(uuid.uuid4())
SESSION_ID = str(uuid.uuid4())
DOCUMENT_ID = str(uuid.uuid4())
VERSION_ID = str(uuid.uuid4())
ASSISTANT_ID = str(uuid.uuid4())
COLLECTION_ID = str(uuid.uuid4())


def _tokens() -> TokenBundle:
    return TokenBundle(
        access_token="test-access",  # noqa: S106 - inert test token
        refresh_token="test-refresh-token-that-is-long-enough",  # noqa: S106
        tenant_id=TENANT_ID,
        access_expires_in=900,
        refresh_expires_in=3600,
    )


class FakeApiClient:
    feedback: ClassVar[list[tuple[str, int, str | None]]] = []
    streamed_requests: ClassVar[list[dict[str, Any]]] = []
    document_calls: ClassVar[int] = 0

    def __init__(self, tokens: TokenBundle) -> None:
        self.tokens = tokens

    def get_me(self) -> dict[str, Any]:
        return {
            "user_id": str(uuid.uuid4()),
            "email": "admin@example.test",
            "is_superuser": False,
            "active_tenant_id": TENANT_ID,
            "memberships": [
                {
                    "tenant_id": TENANT_ID,
                    "slug": "test",
                    "name": "Test organization",
                    "role": "administrator",
                    "active": True,
                }
            ],
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        return [{"id": SESSION_ID, "title": "Test conversation"}]

    def create_session(self, title: str) -> dict[str, Any]:
        return {"id": SESSION_ID, "title": title}

    def delete_session(self, session_id: str) -> None:
        assert session_id == SESSION_ID

    def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        assert session_id == SESSION_ID
        return [
            {
                "id": str(uuid.uuid4()),
                "role": "user",
                "content": "What is the policy?",
                "meta": {},
            },
            {
                "id": ASSISTANT_ID,
                "role": "assistant",
                "content": "The safe answer [S1].",
                "meta": {
                    "citations": {
                        "[S1]": {
                            "marker": "[S1]",
                            "source_id": "S1",
                            "source_type": "document",
                            "title": "Public handbook",
                            "document_id": DOCUMENT_ID,
                            "document_version_id": VERSION_ID,
                            "page_start": 2,
                        }
                    },
                    "retrieval": {"web_status": "failed"},
                },
            },
        ]

    def submit_feedback(
        self,
        message_id: str,
        *,
        rating: int,
        comment: str | None,
    ) -> dict[str, Any]:
        self.feedback.append((message_id, rating, comment))
        return {"id": str(uuid.uuid4())}

    def list_collections(self) -> dict[str, Any]:
        return {
            "items": [
                {
                    "id": COLLECTION_ID,
                    "name": "Public facts",
                    "description": "Public-domain test material",
                }
            ],
            "retrieval_revision": 4,
        }

    def list_documents(self) -> dict[str, Any]:
        type(self).document_calls += 1
        return {
            "items": [
                {
                    "id": DOCUMENT_ID,
                    "title": "Public handbook",
                    "source_filename": "handbook.pdf",
                    "mime": "application/pdf",
                    "active_version_id": VERSION_ID,
                    "collection_ids": [COLLECTION_ID],
                    "versions": [
                        {
                            "id": VERSION_ID,
                            "version": 1,
                            "status": "active",
                            "file_size_bytes": 1024,
                            "page_count": 3,
                            "chunk_count": 8,
                            "error_code": None,
                        }
                    ],
                }
            ],
            "retrieval_revision": 4,
            "active_index_generation_id": 7,
        }

    def create_signed_url(
        self,
        document_id: str,
        *,
        document_version_id: str,
        page: int | None,
    ) -> dict[str, Any]:
        assert (document_id, document_version_id, page) == (
            DOCUMENT_ID,
            VERSION_ID,
            2,
        )
        return {"url": "/api/files/signed-test-token#page=2"}

    def stream_chat(self, **kwargs: Any) -> Any:
        self.streamed_requests.append(kwargs)
        yield SSEEvent("start", {})
        yield SSEEvent("delta", {"text": "unsafe [S9]"})
        yield SSEEvent("replace", {"text": "safe replacement [S1]"})
        yield SSEEvent(
            "citations",
            {
                "items": {
                    "[S1]": {
                        "marker": "[S1]",
                        "source_id": "S1",
                        "source_type": "document",
                        "title": "Public handbook",
                        "document_id": DOCUMENT_ID,
                        "document_version_id": VERSION_ID,
                        "page_start": 2,
                    }
                }
            },
        )
        yield SSEEvent(
            "done",
            {"message_id": ASSISTANT_ID, "route": "rag", "usage": {"total_tokens": 12}},
        )

    def create_collection(self, name: str, description: str | None) -> dict[str, Any]:
        return {"collection": {"id": COLLECTION_ID, "name": name}}

    def update_collection(
        self,
        collection_id: str,
        *,
        name: str,
        description: str | None,
    ) -> dict[str, Any]:
        return {"collection": {"id": collection_id, "name": name}}

    def delete_collection(self, collection_id: str) -> dict[str, Any]:
        return {"changed": True}

    def set_collection_document(
        self,
        collection_id: str,
        document_id: str,
        *,
        present: bool,
    ) -> dict[str, Any]:
        return {"changed": present}

    def upload_document(self, **kwargs: Any) -> dict[str, Any]:
        return {"status": "staged"}

    def reindex_document(self, document_id: str) -> dict[str, Any]:
        return {"status": "staged"}

    def delete_document(self, document_id: str) -> dict[str, Any]:
        return {"retrieval_revision": 5}

    def logout(self) -> None:
        return


def _authenticated_app() -> AppTest:
    app = AppTest.from_file("app.py")
    app.session_state["_auth_tokens"] = _tokens()
    with patch("api_client.ApiClient", FakeApiClient):
        app.run(timeout=10)
    return app


def _element(elements: Any, label: str) -> Any:
    return next(element for element in elements if element.label == label)


def test_login_shell_is_rendered() -> None:
    app = AppTest.from_file("app.py")
    app.run(timeout=10)
    assert not app.exception
    assert app.title[0].value == "Knowledge Assistant"
    assert app.text_input[0].label == "Email"
    assert app.text_input[1].label == "Password"
    assert app.button[0].label == "Sign in"


def test_login_and_logout_replace_and_clear_server_side_auth_state() -> None:
    app = AppTest.from_file("app.py")
    app.run(timeout=10)
    app.text_input[0].set_value("admin@example.test")
    app.text_input[1].set_value("correct horse")
    with (
        patch("api_client.login", return_value=_tokens()),
        patch("api_client.ApiClient", FakeApiClient),
    ):
        app.button[0].click().run(timeout=10)
    assert not app.exception
    assert app.header[0].value == "Chat"
    assert app.session_state["_auth_tokens"].tenant_id == TENANT_ID

    sign_out = _element(app.button, "Sign out")
    with patch("api_client.ApiClient", FakeApiClient):
        sign_out.click().run(timeout=10)
    assert not app.exception
    assert app.text_input[0].label == "Email"
    assert app.session_state["_auth_tokens"] is None


def test_authenticated_chat_shows_web_fallback_citation_feedback_and_stream() -> None:
    FakeApiClient.feedback.clear()
    FakeApiClient.streamed_requests.clear()
    app = _authenticated_app()

    assert not app.exception
    assert app.header[0].value == "Chat"
    assert any("Web search failed" in warning.value for warning in app.warning)
    prepare = _element(app.button, "Prepare [S1] Public handbook, page 2")
    with patch("api_client.ApiClient", FakeApiClient):
        prepare.click().run(timeout=10)
    assert not app.exception
    assert any(link.label.startswith("Open [S1]") for link in app.get("link_button"))

    feedback = _element(app.button, "Helpful")
    with patch("api_client.ApiClient", FakeApiClient):
        feedback.click().run(timeout=10)
    assert FakeApiClient.feedback[-1][:2] == (ASSISTANT_ID, 1)

    app.chat_input[0].set_value("A new question")
    with patch("api_client.ApiClient", FakeApiClient):
        app.run(timeout=10)
    request = FakeApiClient.streamed_requests[-1]
    assert request["message"] == "A new question"
    assert request["idempotency_key"].startswith("chat-")
    assert request["idempotency_key"].removeprefix("chat-") == request["client_request_id"]


def test_documents_and_collections_views_render_admin_controls() -> None:
    FakeApiClient.document_calls = 0
    app = _authenticated_app()
    app.radio[0].set_value("Documents")
    with patch("api_client.ApiClient", FakeApiClient):
        app.run(timeout=10)
    assert not app.exception
    assert app.header[0].value == "Documents"
    assert any(expander.label == "Public handbook" for expander in app.expander)
    polling = _element(app.toggle, "Auto-refresh processing status")
    calls_before_polling = FakeApiClient.document_calls
    polling.set_value(True)
    with patch("api_client.ApiClient", FakeApiClient):
        app.run(timeout=10)
    assert not app.exception
    assert FakeApiClient.document_calls >= calls_before_polling + 2

    app.radio[0].set_value("Collections")
    with patch("api_client.ApiClient", FakeApiClient):
        app.run(timeout=10)
    assert not app.exception
    assert app.header[0].value == "Collections"
    assert any(expander.label == "Public facts" for expander in app.expander)
    assert _element(app.button, "Save document membership")
