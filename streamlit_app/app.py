from __future__ import annotations

import streamlit as st

from api_client import ApiError, login
from state import clear_auth, initialize_state

st.set_page_config(page_title="Knowledge Assistant", page_icon="💬", layout="centered")
initialize_state()

st.title("Knowledge Assistant")
st.caption("Private chat over your organization's approved knowledge sources.")

if st.session_state.access_token:
    st.success("Signed in")
    if st.button("Sign out", type="secondary"):
        clear_auth()
        st.rerun()
else:
    with st.form("login", clear_on_submit=False):
        email = st.text_input("Email", autocomplete="email")
        password = st.text_input("Password", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)

    if submitted:
        if not email or not password:
            st.error("Enter your email and password.")
        else:
            try:
                token = login(email=email, password=password)
            except ApiError:
                st.error("Sign-in failed. Check your details and try again.")
            else:
                st.session_state.access_token = token
                st.rerun()

st.divider()
st.caption("Registration is closed. Contact an administrator if you need an account.")
