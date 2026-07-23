from __future__ import annotations

import uuid
from typing import Any

import streamlit as st

from api_client import ApiClient, ApiError, public_file_url
from sse import ChatAccumulator, validate_citations
from state import signed_links
from views.common import error_message, items, text


def render(client: ApiClient) -> None:
    st.header("Chat")
    try:
        sessions = client.list_sessions()
    except ApiError as exc:
        st.error(error_message(exc, action="load conversations"))
        return

    selected_id = _session_sidebar(client, sessions)
    if selected_id is None:
        st.info("Start a conversation from the sidebar.")
        return

    selected = next(
        (session for session in sessions if str(session.get("id")) == selected_id),
        None,
    )
    st.caption(text(selected.get("title")) if selected else "Conversation")

    try:
        collection_payload = client.list_collections()
        document_payload = client.list_documents()
        messages = client.list_messages(selected_id)
    except ApiError as exc:
        st.error(error_message(exc, action="load this conversation"))
        return

    collections = items(collection_payload)
    documents = items(document_payload)
    _scope_controls(collections, documents)
    if not messages:
        st.info("No messages yet. Ask a question about the selected knowledge sources.")
    for message in messages:
        _render_message(client, message)

    prompt = st.chat_input("Ask about your knowledge sources")
    if prompt:
        _send_message(client, selected_id, prompt)


def _session_sidebar(
    client: ApiClient,
    sessions: list[dict[str, Any]],
) -> str | None:
    selected_id = st.session_state.get("selected_session_id")
    known_ids = {str(session.get("id")) for session in sessions}
    if selected_id not in known_ids:
        selected_id = str(sessions[0].get("id")) if sessions else None
        st.session_state["selected_session_id"] = selected_id

    with st.sidebar:
        st.subheader("Conversations")
        with st.form("new_conversation", clear_on_submit=True):
            title = st.text_input(
                "Conversation title",
                placeholder="e.g. Product handbook",
                max_chars=240,
            )
            create = st.form_submit_button("New conversation", use_container_width=True)
        if create:
            if not title.strip():
                st.error("Enter a conversation title.")
            else:
                try:
                    created = client.create_session(title.strip())
                except ApiError as exc:
                    st.error(error_message(exc, action="create a conversation"))
                else:
                    st.session_state["selected_session_id"] = str(created.get("id"))
                    st.rerun()

        for session in sessions:
            session_id = str(session.get("id"))
            label = text(session.get("title"), "Untitled conversation")
            if st.button(
                label,
                key=f"session-{session_id}",
                type="primary" if session_id == selected_id else "secondary",
                use_container_width=True,
            ):
                st.session_state["selected_session_id"] = session_id
                st.rerun()

        if selected_id and st.button(
            "Delete current conversation",
            key="delete-current-session",
            use_container_width=True,
        ):
            try:
                client.delete_session(selected_id)
            except ApiError as exc:
                st.error(error_message(exc, action="delete the conversation"))
            else:
                st.session_state["selected_session_id"] = None
                st.rerun()
    return selected_id


def _scope_controls(
    collections: list[dict[str, Any]],
    documents: list[dict[str, Any]],
) -> None:
    collection_options = {
        str(item.get("id")): text(item.get("name"), "Untitled collection") for item in collections
    }
    document_options = {
        str(item.get("id")): text(item.get("title"), "Untitled document") for item in documents
    }
    valid_collections = [
        value
        for value in st.session_state.get("chat_collection_ids", [])
        if value in collection_options
    ]
    valid_documents = [
        value
        for value in st.session_state.get("chat_document_ids", [])
        if value in document_options
    ]
    st.session_state["chat_collection_ids"] = valid_collections
    st.session_state["chat_document_ids"] = valid_documents

    with st.expander("Knowledge scope", expanded=not (collections or documents)):
        st.multiselect(
            "Collections",
            options=list(collection_options),
            format_func=lambda value: collection_options[value],
            key="chat_collection_ids",
            help="Leave empty to search across collections you can access.",
        )
        st.multiselect(
            "Documents",
            options=list(document_options),
            format_func=lambda value: document_options[value],
            key="chat_document_ids",
            help="Use this to narrow a question to specific documents.",
        )
        st.checkbox(
            "Search the web too",
            key="chat_web_search",
            help="Document evidence remains available if web search fails.",
        )


def _render_message(client: ApiClient, message: dict[str, Any]) -> None:
    role = message.get("role")
    if role not in {"user", "assistant"}:
        return
    message_id = str(message.get("id"))
    with st.chat_message(role):
        st.markdown(text(message.get("content"), ""))
        if role == "assistant":
            meta = message.get("meta")
            metadata = meta if isinstance(meta, dict) else {}
            _render_web_status(metadata)
            _render_citations(
                client,
                message_id=message_id,
                citation_value=metadata.get("citations"),
            )
            _render_feedback(client, message_id)


def _render_web_status(metadata: dict[str, Any]) -> None:
    retrieval = metadata.get("retrieval")
    if not isinstance(retrieval, dict):
        return
    status = retrieval.get("web_status")
    if status == "failed":
        st.warning("Web search failed; this answer used available document evidence.")
    elif status == "partial":
        st.warning("Some web sources failed; this answer used the evidence that remained.")
    elif status == "empty":
        st.info("Web search found no usable pages; document evidence was still considered.")


def _render_citations(
    client: ApiClient,
    *,
    message_id: str,
    citation_value: object,
) -> None:
    citations = validate_citations(citation_value)
    if not citations:
        return
    st.caption("Sources")
    for marker, citation in citations.items():
        title = text(citation.get("title"), "Source")
        page_start = citation.get("page_start")
        page_end = citation.get("page_end")
        page_label = ""
        if isinstance(page_start, int):
            page_label = (
                f", pages {page_start}-{page_end}"
                if isinstance(page_end, int) and page_end != page_start
                else f", page {page_start}"
            )
        label = f"{marker} {title}{page_label}"
        if citation.get("source_type") == "web":
            st.link_button(
                label,
                text(citation.get("uri")),
                help=f"Web source for answer {message_id}",
            )
            continue

        link_key = f"{message_id}:{marker}"
        cached = signed_links().get(link_key)
        if isinstance(cached, dict) and isinstance(cached.get("url"), str):
            try:
                target = public_file_url(cached["url"])
            except ApiError:
                signed_links().pop(link_key, None)
            else:
                st.link_button(
                    f"Open {label}",
                    target,
                    help=f"Document source for answer {message_id}",
                )
                continue
        if st.button(
            f"Prepare {label}",
            key=f"prepare-source-{message_id}-{marker}",
        ):
            try:
                response = client.create_signed_url(
                    text(citation.get("document_id")),
                    document_version_id=text(citation.get("document_version_id")),
                    page=page_start if isinstance(page_start, int) else None,
                )
                public_file_url(text(response.get("url")))
            except ApiError as exc:
                st.error(error_message(exc, action="open that source"))
            else:
                signed_links()[link_key] = response
                st.rerun()


def _render_feedback(client: ApiClient, message_id: str) -> None:
    sent = st.session_state.get("feedback_sent", {})
    if isinstance(sent, dict) and message_id in sent:
        st.caption("Feedback saved.")
    comment = st.text_input(
        "Optional feedback",
        key=f"feedback-comment-{message_id}",
        max_chars=2000,
        label_visibility="collapsed",
        placeholder="Optional feedback about this answer",
    )
    positive, negative = st.columns(2)
    rating: int | None = None
    if positive.button("Helpful", key=f"helpful-{message_id}", use_container_width=True):
        rating = 1
    if negative.button(
        "Not helpful",
        key=f"not-helpful-{message_id}",
        use_container_width=True,
    ):
        rating = -1
    if rating is None:
        return
    try:
        client.submit_feedback(
            message_id,
            rating=rating,
            comment=comment,
        )
    except ApiError as exc:
        st.error(error_message(exc, action="save feedback"))
    else:
        if isinstance(sent, dict):
            sent[message_id] = rating
        st.success("Feedback saved.")


def _send_message(client: ApiClient, session_id: str, prompt: str) -> None:
    request_id = str(uuid.uuid4())
    idempotency_key = f"chat-{request_id}"
    accumulator = ChatAccumulator()
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        answer = st.empty()
        progress = st.empty()
        try:
            for event in client.stream_chat(
                session_id=session_id,
                message=prompt,
                collection_ids=list(st.session_state.get("chat_collection_ids", [])),
                document_ids=list(st.session_state.get("chat_document_ids", [])),
                web_search=bool(st.session_state.get("chat_web_search", False)),
                client_request_id=request_id,
                idempotency_key=idempotency_key,
            ):
                accumulator.apply(event)
                if event.event == "status" and accumulator.stages:
                    progress.caption(f"{accumulator.stages[-1].capitalize()}…")
                if event.event in {"delta", "replace"}:
                    answer.markdown(accumulator.answer)
        except ApiError as exc:
            progress.empty()
            st.error(error_message(exc, action="finish the answer"))
            return
        progress.empty()
        if accumulator.error_code is not None:
            st.error(
                "The answer could not be completed."
                + (" You can retry safely." if accumulator.retryable else "")
            )
            return
        if not accumulator.done:
            st.error("The answer stream ended unexpectedly. You can retry safely.")
            return
        answer.markdown(accumulator.answer)
    st.rerun()
