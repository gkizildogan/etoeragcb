from __future__ import annotations

import os

import httpx

API_INTERNAL_URL = os.environ.get("API_INTERNAL_URL", "http://backend:8000/api").rstrip("/")


class ApiError(RuntimeError):
    pass


def login(*, email: str, password: str) -> str:
    try:
        response = httpx.post(
            f"{API_INTERNAL_URL}/auth/login",
            json={"email": email, "password": password},
            timeout=10,
        )
        response.raise_for_status()
        token = response.json()["access_token"]
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise ApiError("login request failed") from exc
    if not isinstance(token, str) or not token:
        raise ApiError("login response did not contain a token")
    return token
