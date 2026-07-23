from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from sse import SSEEvent, SSEProtocolError, parse_sse

API_INTERNAL_URL = os.environ.get("API_INTERNAL_URL", "http://backend:8000/api").rstrip("/")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8501").rstrip("/")
REQUEST_TIMEOUT = 15.0
STREAM_TIMEOUT = 240.0


class ApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class AuthenticationExpired(ApiError):
    pass


@dataclass(slots=True)
class TokenBundle:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    tenant_id: str
    access_expires_in: int
    refresh_expires_in: int


def login(
    *,
    email: str,
    password: str,
    tenant_id: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> TokenBundle:
    payload: dict[str, object] = {"email": email, "password": password}
    if tenant_id is not None:
        payload["tenant_id"] = tenant_id
    try:
        with httpx.Client(
            base_url=API_INTERNAL_URL,
            timeout=REQUEST_TIMEOUT,
            transport=transport,
        ) as client:
            response = client.post("/auth/login", json=payload)
    except httpx.HTTPError as exc:
        raise ApiError("login_unavailable") from exc
    if response.status_code != 200:
        raise _api_error(response, "login_failed")
    return _token_bundle(response)


class ApiClient:
    def __init__(
        self,
        tokens: TokenBundle,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.tokens = tokens
        self._transport = transport

    def get_me(self) -> dict[str, Any]:
        return self._json("GET", "/me")

    def list_sessions(self) -> list[dict[str, Any]]:
        return self._all_pages("/sessions", item_limit=25)

    def create_session(self, title: str) -> dict[str, Any]:
        return self._json("POST", "/sessions", json={"title": title})

    def delete_session(self, session_id: str) -> None:
        self._request("DELETE", f"/sessions/{session_id}", expected={204})

    def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        return self._all_pages(
            f"/sessions/{session_id}/messages",
            item_limit=50,
        )

    def submit_feedback(
        self,
        message_id: str,
        *,
        rating: int,
        comment: str | None,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/messages/{message_id}/feedback",
            json={"rating": rating, "comment": comment or None},
        )

    def list_collections(self) -> dict[str, Any]:
        return self._json("GET", "/collections")

    def create_collection(self, name: str, description: str | None) -> dict[str, Any]:
        return self._json(
            "POST",
            "/collections",
            json={"name": name, "description": description or None},
        )

    def update_collection(
        self,
        collection_id: str,
        *,
        name: str,
        description: str | None,
    ) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/collections/{collection_id}",
            json={"name": name, "description": description or None},
        )

    def delete_collection(self, collection_id: str) -> dict[str, Any]:
        return self._json("DELETE", f"/collections/{collection_id}")

    def set_collection_document(
        self,
        collection_id: str,
        document_id: str,
        *,
        present: bool,
    ) -> dict[str, Any]:
        method = "PUT" if present else "DELETE"
        return self._json(
            method,
            f"/collections/{collection_id}/documents/{document_id}",
        )

    def list_documents(self) -> dict[str, Any]:
        return self._json("GET", "/documents")

    def upload_document(
        self,
        *,
        filename: str,
        content: bytes,
        mime: str,
        title: str,
        collection_ids: list[str],
        document_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        data = {
            "title": title,
            "collection_ids_json": json.dumps(collection_ids),
        }
        if document_id is not None:
            data["document_id"] = document_id
        key = idempotency_key or f"upload-{uuid.uuid4()}"
        response = self._request(
            "POST",
            "/documents",
            expected={202},
            headers={"Idempotency-Key": key},
            data=data,
            files={"file": (filename, content, mime)},
            timeout=120.0,
        )
        return _json_object(response)

    def reindex_document(
        self,
        document_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/documents/{document_id}/reindex",
            headers={"Idempotency-Key": idempotency_key or f"reindex-{uuid.uuid4()}"},
            expected={202},
        )

    def delete_document(self, document_id: str) -> dict[str, Any]:
        return self._json("DELETE", f"/documents/{document_id}")

    def create_signed_url(
        self,
        document_id: str,
        *,
        document_version_id: str,
        page: int | None,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/documents/{document_id}/signed-url",
            json={
                "document_version_id": document_version_id,
                "page": page,
            },
        )

    def stream_chat(
        self,
        *,
        session_id: str,
        message: str,
        collection_ids: list[str],
        document_ids: list[str],
        web_search: bool,
        client_request_id: str,
        idempotency_key: str,
        max_reconnects: int = 1,
    ) -> Iterator[SSEEvent]:
        body = {
            "session_id": session_id,
            "message": message,
            "collection_ids": collection_ids,
            "document_ids": document_ids,
            "web_search": web_search,
            "client_request_id": client_request_id,
        }
        reconnects = 0
        refreshed = False
        while True:
            terminal = False
            try:
                with self._client(timeout=STREAM_TIMEOUT) as client:
                    with client.stream(
                        "POST",
                        "/chat",
                        json=body,
                        headers=self._headers({"Idempotency-Key": idempotency_key}),
                    ) as response:
                        if response.status_code == 401 and not refreshed:
                            refreshed = True
                            self._refresh()
                            continue
                        if response.status_code == 401:
                            raise AuthenticationExpired(
                                "session_expired",
                                status_code=response.status_code,
                            )
                        if response.status_code == 425 and reconnects < max_reconnects:
                            reconnects += 1
                            time.sleep(min(_retry_after(response) or 1, 2))
                            continue
                        if response.status_code != 200:
                            response.read()
                            raise _api_error(response, "chat_failed")
                        for event in parse_sse(response.iter_lines()):
                            yield event
                            if event.event in {"done", "error"}:
                                terminal = True
                if terminal:
                    return
                raise SSEProtocolError("chat stream ended without a terminal event")
            except AuthenticationExpired:
                raise
            except (httpx.TransportError, SSEProtocolError) as exc:
                if reconnects >= max_reconnects:
                    raise ApiError("chat_stream_interrupted") from exc
                reconnects += 1

    def logout(self) -> None:
        raw_refresh = self.tokens.refresh_token
        try:
            with self._client(timeout=REQUEST_TIMEOUT) as client:
                client.post("/auth/logout", json={"refresh_token": raw_refresh})
        except httpx.HTTPError:
            return

    def _all_pages(self, path: str, *, item_limit: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _page in range(100):
            params: dict[str, object] = {"limit": item_limit}
            if cursor is not None:
                params["cursor"] = cursor
            payload = self._json("GET", path, params=params)
            page_items = payload.get("items")
            if not isinstance(page_items, list):
                raise ApiError("invalid_page")
            for item in page_items:
                if not isinstance(item, dict):
                    raise ApiError("invalid_page")
                items.append(item)
            next_cursor = payload.get("next_cursor")
            if next_cursor is None:
                return items
            if not isinstance(next_cursor, str) or not next_cursor:
                raise ApiError("invalid_page")
            cursor = next_cursor
        raise ApiError("page_limit_exceeded")

    def _json(
        self,
        method: str,
        path: str,
        *,
        expected: set[int] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return _json_object(
            self._request(
                method,
                path,
                expected=expected or {200, 201},
                **kwargs,
            )
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected: set[int],
        headers: dict[str, str] | None = None,
        timeout: float = REQUEST_TIMEOUT,
        **kwargs: Any,
    ) -> httpx.Response:
        refreshed = False
        while True:
            try:
                with self._client(timeout=timeout) as client:
                    response = client.request(
                        method,
                        path,
                        headers=self._headers(headers),
                        **kwargs,
                    )
            except httpx.HTTPError as exc:
                raise ApiError("api_unavailable") from exc
            if response.status_code == 401 and not refreshed:
                refreshed = True
                self._refresh()
                continue
            if response.status_code == 401:
                raise AuthenticationExpired(
                    "session_expired",
                    status_code=response.status_code,
                )
            if response.status_code not in expected:
                raise _api_error(response, "api_request_failed")
            return response

    def _refresh(self) -> None:
        try:
            with self._client(timeout=REQUEST_TIMEOUT) as client:
                response = client.post(
                    "/auth/refresh",
                    json={"refresh_token": self.tokens.refresh_token},
                )
        except httpx.HTTPError as exc:
            raise AuthenticationExpired("refresh_unavailable") from exc
        if response.status_code != 200:
            raise AuthenticationExpired(
                "session_expired",
                status_code=response.status_code,
            )
        replacement = _token_bundle(response)
        self.tokens.access_token = replacement.access_token
        self.tokens.refresh_token = replacement.refresh_token
        self.tokens.tenant_id = replacement.tenant_id
        self.tokens.access_expires_in = replacement.access_expires_in
        self.tokens.refresh_expires_in = replacement.refresh_expires_in

    def _headers(self, values: dict[str, str] | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.tokens.access_token}",
            **(values or {}),
        }

    def _client(self, *, timeout: float) -> httpx.Client:
        return httpx.Client(
            base_url=API_INTERNAL_URL,
            timeout=timeout,
            transport=self._transport,
        )


def public_file_url(relative_url: str) -> str:
    parsed = urlsplit(relative_url)
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/api/files/")
        or parsed.path.startswith("//")
    ):
        raise ApiError("invalid_signed_url")
    return urljoin(f"{PUBLIC_BASE_URL}/", relative_url.lstrip("/"))


def _token_bundle(response: httpx.Response) -> TokenBundle:
    payload = _json_object(response)
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    tenant = payload.get("tenant_id")
    access_ttl = payload.get("expires_in")
    refresh_ttl = payload.get("refresh_expires_in")
    if (
        not isinstance(access, str)
        or not access
        or not isinstance(refresh, str)
        or not refresh
        or not isinstance(tenant, str)
        or not tenant
        or not isinstance(access_ttl, int)
        or not isinstance(refresh_ttl, int)
    ):
        raise ApiError("invalid_token_response")
    return TokenBundle(
        access_token=access,
        refresh_token=refresh,
        tenant_id=tenant,
        access_expires_in=access_ttl,
        refresh_expires_in=refresh_ttl,
    )


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError as exc:
        raise ApiError("invalid_api_response", status_code=response.status_code) from exc
    if not isinstance(value, dict):
        raise ApiError("invalid_api_response", status_code=response.status_code)
    return value


def _api_error(response: httpx.Response, fallback: str) -> ApiError:
    return ApiError(
        fallback,
        status_code=response.status_code,
        retry_after=_retry_after(response),
    )


def _retry_after(response: httpx.Response) -> int | None:
    raw = response.headers.get("retry-after")
    try:
        value = int(raw) if raw is not None else None
    except ValueError:
        return None
    return value if value is not None and value > 0 else None
