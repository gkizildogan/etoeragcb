from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit as st

from api_client import ApiClient, ApiError
from views.common import error_message, is_admin, items, text

MIME_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".jsonl": "application/x-ndjson",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def render(client: ApiClient, profile: dict[str, Any]) -> None:
    st.header("Documents")
    administrator = is_admin(profile)
    try:
        payload = client.list_documents()
        collections = items(client.list_collections())
    except ApiError as exc:
        st.error(error_message(exc, action="load documents"))
        return
    documents = items(payload)

    if administrator:
        _upload(client, documents, collections)
    else:
        st.info("Members can view documents and ingestion status. An administrator manages files.")

    st.toggle(
        "Auto-refresh processing status",
        key="document_polling",
        help="Refreshes this inventory every five seconds while work is in progress.",
    )

    @st.fragment(run_every="5s" if st.session_state.document_polling else None)
    def inventory() -> None:
        try:
            latest = (
                items(client.list_documents()) if st.session_state.document_polling else documents
            )
        except ApiError as exc:
            st.error(error_message(exc, action="refresh document status"))
            return
        _inventory(client, latest, administrator)

    inventory()


def _upload(
    client: ApiClient,
    documents: list[dict[str, Any]],
    collections: list[dict[str, Any]],
) -> None:
    with st.expander("Upload a document or new version"):
        document_options = {"new": "New document"}
        document_options.update(
            {
                str(document.get("id")): f"New version of {text(document.get('title'), 'document')}"
                for document in documents
            }
        )
        target = st.selectbox(
            "Upload type",
            options=list(document_options),
            format_func=lambda value: document_options[value],
            key="upload-target",
        )
        existing = next(
            (document for document in documents if str(document.get("id")) == target),
            None,
        )
        collection_options = {
            str(item.get("id")): text(item.get("name"), "Untitled collection")
            for item in collections
        }
        with st.form("document-upload", clear_on_submit=True):
            title = st.text_input(
                "Title",
                value=text(existing.get("title")) if existing else "",
                max_chars=300,
            )
            selected_collections: list[str] = []
            if existing is None:
                selected_collections = st.multiselect(
                    "Collections",
                    options=list(collection_options),
                    format_func=lambda value: collection_options[value],
                )
            uploaded = st.file_uploader(
                "File",
                type=["pdf", "txt", "md", "jsonl", "docx"],
            )
            submit = st.form_submit_button("Upload", type="primary")
        if submit:
            if uploaded is None or not title.strip():
                st.error("Choose a file and enter a title.")
                return
            suffix = Path(uploaded.name).suffix.casefold()
            mime = MIME_BY_SUFFIX.get(suffix, uploaded.type or "application/octet-stream")
            try:
                client.upload_document(
                    filename=uploaded.name,
                    content=uploaded.getvalue(),
                    mime=mime,
                    title=title.strip(),
                    collection_ids=(
                        [str(value) for value in existing.get("collection_ids", [])]
                        if existing
                        else selected_collections
                    ),
                    document_id=str(existing.get("id")) if existing else None,
                )
            except ApiError as exc:
                st.error(error_message(exc, action="upload the document"))
            else:
                st.session_state["document_polling"] = True
                st.session_state["flash_message"] = "Upload accepted. Ingestion has started."
                st.rerun()


def _inventory(
    client: ApiClient,
    documents: list[dict[str, Any]],
    administrator: bool,
) -> None:
    if not documents:
        st.info("No documents have been uploaded.")
        return
    processing = False
    for document in documents:
        document_id = str(document.get("id"))
        title = text(document.get("title"), "Untitled document")
        versions = document.get("versions")
        valid_versions = (
            [version for version in versions if isinstance(version, dict)]
            if isinstance(versions, list)
            else []
        )
        with st.expander(title):
            st.caption(
                f"{text(document.get('source_filename'), 'Unknown file')} · "
                f"{text(document.get('mime'), 'unknown type')}"
            )
            active_id = str(document.get("active_version_id") or "")
            for version in valid_versions:
                version_id = str(version.get("id"))
                status = text(version.get("status"), "unknown")
                processing = processing or status in {"staged", "processing", "ready"}
                active = " · active" if version_id == active_id else ""
                st.markdown(
                    f"**Version {version.get('version', '?')}** · `{status}`{active}  \n"
                    f"{version.get('page_count', 0)} pages · "
                    f"{version.get('chunk_count', 0)} chunks · "
                    f"{_size(version.get('file_size_bytes'))}"
                )
                if status == "failed":
                    code = text(version.get("error_code"), "ingestion_failed")
                    st.error(f"Ingestion failed: {code}")

            if administrator:
                reindex, remove = st.columns(2)
                if reindex.button(
                    "Reindex active version",
                    key=f"reindex-{document_id}",
                    use_container_width=True,
                    disabled=not bool(active_id),
                ):
                    try:
                        client.reindex_document(document_id)
                    except ApiError as exc:
                        st.error(error_message(exc, action="start reindexing"))
                    else:
                        st.session_state["document_polling"] = True
                        st.rerun()
                confirm = remove.checkbox(
                    "Confirm delete",
                    key=f"confirm-document-delete-{document_id}",
                )
                if remove.button(
                    "Delete document",
                    key=f"delete-document-{document_id}",
                    use_container_width=True,
                    disabled=not confirm,
                ):
                    try:
                        client.delete_document(document_id)
                    except ApiError as exc:
                        st.error(error_message(exc, action="delete the document"))
                    else:
                        st.rerun()
    if processing and not st.session_state.document_polling:
        st.caption("Some versions are still processing. Enable auto-refresh to follow progress.")


def _size(raw: object) -> str:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        return "unknown size"
    if raw < 1024:
        return f"{raw} B"
    if raw < 1024 * 1024:
        return f"{raw / 1024:.1f} KiB"
    return f"{raw / (1024 * 1024):.1f} MiB"
