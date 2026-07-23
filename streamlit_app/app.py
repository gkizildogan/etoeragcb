from __future__ import annotations

from typing import Any

import streamlit as st

import api_client
from state import auth_tokens, clear_auth, initialize_state, set_auth
from views import chat, collections, documents
from views.common import active_role, error_message, flash, text

st.set_page_config(page_title="Knowledge Assistant", page_icon="💬", layout="wide")
initialize_state()

st.title("Knowledge Assistant")
st.caption("Private chat over your organization's approved knowledge sources.")
flash()

tokens = auth_tokens()
if tokens is None:
    with st.form("login", clear_on_submit=False):
        email = st.text_input("Email", autocomplete="email")
        password = st.text_input(
            "Password",
            type="password",
            autocomplete="current-password",
        )
        submitted = st.form_submit_button(
            "Sign in",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        if not email or not password:
            st.error("Enter your email and password.")
        else:
            try:
                replacement = api_client.login(email=email, password=password)
            except api_client.ApiError as exc:
                st.error(error_message(exc, action="sign in"))
            else:
                set_auth(replacement)
                st.rerun()

    st.divider()
    st.caption("Registration is closed. Contact an administrator if you need an account.")
    st.stop()

client = api_client.ApiClient(tokens)
try:
    profile = client.get_me()
except api_client.AuthenticationExpired:
    clear_auth(message="Your session expired. Sign in again.")
    st.rerun()
except api_client.ApiError as exc:
    st.error(error_message(exc, action="load your account"))
    if st.button("Retry"):
        st.rerun()
    st.stop()


def _active_memberships(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        item
        for item in value
        if isinstance(item, dict) and item.get("active") is True
    ]


with st.sidebar:
    st.write(text(profile.get("email"), "Signed in"))
    st.caption(f"Role: {active_role(profile)}")
    view = st.radio(
        "View",
        ["Chat", "Documents", "Collections"],
        key="selected_view",
    )
    memberships = _active_memberships(profile.get("memberships"))
    if len(memberships) > 1:
        with st.expander("Switch organization"):
            membership_labels = {
                str(item.get("tenant_id")): text(item.get("name"), text(item.get("slug")))
                for item in memberships
            }
            tenant_ids = list(membership_labels)
            current_index = (
                tenant_ids.index(tokens.tenant_id)
                if tokens.tenant_id in tenant_ids
                else 0
            )
            with st.form("switch-organization", clear_on_submit=True):
                target_tenant = st.selectbox(
                    "Organization",
                    options=tenant_ids,
                    index=current_index,
                    format_func=lambda value: membership_labels[value],
                )
                switch_password = st.text_input(
                    "Password",
                    type="password",
                    autocomplete="current-password",
                )
                switch = st.form_submit_button(
                    "Switch",
                    disabled=target_tenant == tokens.tenant_id,
                    use_container_width=True,
                )
            if switch:
                try:
                    replacement = api_client.login(
                        email=text(profile.get("email")),
                        password=switch_password,
                        tenant_id=target_tenant,
                    )
                except api_client.ApiError as exc:
                    st.error(error_message(exc, action="switch organizations"))
                else:
                    client.logout()
                    set_auth(replacement)
                    st.rerun()
    if st.button("Sign out", type="secondary", use_container_width=True):
        client.logout()
        clear_auth(message="Signed out.")
        st.rerun()

try:
    if view == "Chat":
        chat.render(client)
    elif view == "Documents":
        documents.render(client, profile)
    else:
        collections.render(client, profile)
except api_client.AuthenticationExpired:
    clear_auth(message="Your session expired. Sign in again.")
    st.rerun()
