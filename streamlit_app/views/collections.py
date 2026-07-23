from __future__ import annotations

from typing import Any

import streamlit as st

from api_client import ApiClient, ApiError
from views.common import error_message, is_admin, items, text


def render(client: ApiClient, profile: dict[str, Any]) -> None:
    st.header("Collections")
    administrator = is_admin(profile)
    try:
        payload = client.list_collections()
        documents = items(client.list_documents())
    except ApiError as exc:
        st.error(error_message(exc, action="load collections"))
        return
    collections = items(payload)
    revision = payload.get("retrieval_revision")
    if isinstance(revision, int):
        st.caption(f"Retrieval revision {revision}")

    if administrator:
        _create(client)
    else:
        st.info("Members can view collections. An administrator manages their contents.")

    if not collections:
        st.info("No collections have been created.")
        return
    for collection in collections:
        _collection(client, collection, documents, administrator)


def _create(client: ApiClient) -> None:
    with st.expander("Create a collection"):
        with st.form("create-collection", clear_on_submit=True):
            name = st.text_input("Name", max_chars=200)
            description = st.text_area("Description", max_chars=4000)
            submit = st.form_submit_button("Create collection", type="primary")
        if submit:
            if not name.strip():
                st.error("Enter a collection name.")
                return
            try:
                client.create_collection(name.strip(), description)
            except ApiError as exc:
                st.error(error_message(exc, action="create the collection"))
            else:
                st.rerun()


def _collection(
    client: ApiClient,
    collection: dict[str, Any],
    documents: list[dict[str, Any]],
    administrator: bool,
) -> None:
    collection_id = str(collection.get("id"))
    name = text(collection.get("name"), "Untitled collection")
    with st.expander(name):
        if not administrator:
            description = text(collection.get("description"))
            st.write(description or "No description.")
            members = [
                text(document.get("title"), "Untitled document")
                for document in documents
                if collection_id
                in {str(value) for value in document.get("collection_ids", [])}
            ]
            st.write(", ".join(members) if members else "No documents in this collection.")
            return

        with st.form(f"edit-collection-{collection_id}"):
            updated_name = st.text_input(
                "Name",
                value=name,
                max_chars=200,
                key=f"collection-name-{collection_id}",
            )
            updated_description = st.text_area(
                "Description",
                value=text(collection.get("description")),
                max_chars=4000,
                key=f"collection-description-{collection_id}",
            )
            update = st.form_submit_button("Save details")
        if update:
            if not updated_name.strip():
                st.error("Enter a collection name.")
            else:
                try:
                    client.update_collection(
                        collection_id,
                        name=updated_name.strip(),
                        description=updated_description,
                    )
                except ApiError as exc:
                    st.error(error_message(exc, action="update the collection"))
                else:
                    st.rerun()

        document_options = {
            str(document.get("id")): text(document.get("title"), "Untitled document")
            for document in documents
        }
        current = {
            document_id
            for document_id, _label in document_options.items()
            if collection_id
            in {
                str(value)
                for value in next(
                    (
                        document.get("collection_ids", [])
                        for document in documents
                        if str(document.get("id")) == document_id
                    ),
                    [],
                )
            }
        }
        selected = st.multiselect(
            "Documents",
            options=list(document_options),
            default=sorted(current),
            format_func=lambda value: document_options[value],
            key=f"collection-documents-{collection_id}",
        )
        if st.button(
            "Save document membership",
            key=f"save-membership-{collection_id}",
        ):
            desired = set(selected)
            try:
                for document_id in sorted(desired - current):
                    client.set_collection_document(
                        collection_id,
                        document_id,
                        present=True,
                    )
                for document_id in sorted(current - desired):
                    client.set_collection_document(
                        collection_id,
                        document_id,
                        present=False,
                    )
            except ApiError as exc:
                st.error(error_message(exc, action="update document membership"))
            else:
                st.rerun()

        confirm = st.checkbox(
            "Confirm collection deletion",
            key=f"confirm-collection-delete-{collection_id}",
        )
        if st.button(
            "Delete collection",
            key=f"delete-collection-{collection_id}",
            disabled=not confirm,
        ):
            try:
                client.delete_collection(collection_id)
            except ApiError as exc:
                st.error(error_message(exc, action="delete the collection"))
            else:
                st.rerun()
