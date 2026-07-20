from __future__ import annotations

import streamlit as st


def initialize_state() -> None:
    st.session_state.setdefault("access_token", None)


def clear_auth() -> None:
    st.session_state.access_token = None
