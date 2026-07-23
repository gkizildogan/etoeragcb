from __future__ import annotations

from typing import Any

import streamlit as st

from api_client import ApiError


def active_role(profile: dict[str, Any]) -> str:
    active_tenant = str(profile.get("active_tenant_id", ""))
    memberships = profile.get("memberships")
    if not isinstance(memberships, list):
        return "member"
    for membership in memberships:
        if (
            isinstance(membership, dict)
            and str(membership.get("tenant_id", "")) == active_tenant
        ):
            role = membership.get("role")
            return role if isinstance(role, str) else "member"
    return "member"


def is_admin(profile: dict[str, Any]) -> bool:
    return active_role(profile) == "administrator" or profile.get("is_superuser") is True


def error_message(error: ApiError, *, action: str = "complete that request") -> str:
    if error.status_code == 403:
        return "Your account does not have permission to do that."
    if error.status_code == 404:
        return "That item is no longer available."
    if error.status_code == 409:
        return "That change conflicts with the current data. Refresh and try again."
    if error.status_code == 413:
        return "That file is too large."
    if error.status_code == 422:
        return "The server could not validate that input."
    if error.status_code == 425:
        return "The same request is still being processed. Try again shortly."
    if error.status_code == 429:
        suffix = (
            f" Try again in about {error.retry_after} seconds."
            if error.retry_after is not None
            else " Try again shortly."
        )
        return f"Too many requests.{suffix}"
    if str(error) in {"api_unavailable", "login_unavailable"}:
        return "The service is temporarily unavailable. Try again shortly."
    return f"Could not {action}. Try again."


def text(value: object, fallback: str = "") -> str:
    return value if isinstance(value, str) else fallback


def items(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return []
    return [item for item in raw_items if isinstance(item, dict)]


def flash() -> None:
    value = st.session_state.get("flash_message")
    if isinstance(value, str) and value:
        st.info(value)
        st.session_state["flash_message"] = None
