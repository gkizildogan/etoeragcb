from __future__ import annotations

from typing import Any

import streamlit as st

from api_client import TokenBundle

AUTH_KEY = "_auth_tokens"
TENANT_SCOPED_KEYS = (
    "selected_session_id",
    "chat_collection_ids",
    "chat_document_ids",
    "chat_web_search",
    "signed_links",
    "feedback_sent",
    "document_polling",
    "upload-target",
)


def initialize_state() -> None:
    st.session_state.setdefault(AUTH_KEY, None)
    st.session_state.setdefault("selected_view", "Chat")
    st.session_state.setdefault("selected_session_id", None)
    st.session_state.setdefault("chat_collection_ids", [])
    st.session_state.setdefault("chat_document_ids", [])
    st.session_state.setdefault("signed_links", {})
    st.session_state.setdefault("feedback_sent", {})
    st.session_state.setdefault("document_polling", False)
    st.session_state.setdefault("flash_message", None)


def auth_tokens() -> TokenBundle | None:
    value = st.session_state.get(AUTH_KEY)
    return value if isinstance(value, TokenBundle) else None


def set_auth(tokens: TokenBundle) -> None:
    st.session_state[AUTH_KEY] = tokens
    clear_tenant_state()


def clear_tenant_state() -> None:
    for key in TENANT_SCOPED_KEYS:
        if key in {"chat_collection_ids", "chat_document_ids"}:
            st.session_state[key] = []
        elif key in {"signed_links", "feedback_sent"}:
            st.session_state[key] = {}
        elif key in {"document_polling", "chat_web_search"}:
            st.session_state[key] = False
        elif key == "upload-target":
            st.session_state[key] = "new"
        else:
            st.session_state[key] = None
    for key in list(st.session_state):
        if key.startswith(
            (
                "feedback-comment-",
                "collection-name-",
                "collection-description-",
                "collection-documents-",
                "confirm-document-delete-",
                "confirm-collection-delete-",
            )
        ):
            del st.session_state[key]


def clear_auth(*, message: str | None = None) -> None:
    st.session_state[AUTH_KEY] = None
    clear_tenant_state()
    st.session_state["flash_message"] = message


def signed_links() -> dict[str, dict[str, Any]]:
    value = st.session_state.get("signed_links")
    if not isinstance(value, dict):
        value = {}
        st.session_state["signed_links"] = value
    return value
