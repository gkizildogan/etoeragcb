from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from app.chat.schemas import UsageSummary


class GenerationError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class GenerationChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str = ""
    usage: UsageSummary | None = None


class VllmGenerator:
    """A bounded content-only client for vLLM's OpenAI-compatible stream."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        max_tokens: int,
        max_concurrency: int,
        timeout_seconds: float = 180.0,
        queue_timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._timeout = timeout_seconds
        self._queue_timeout = queue_timeout_seconds
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"))
        self._owns_client = client is None

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[GenerationChunk]:
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._queue_timeout)
        except TimeoutError as exc:
            raise GenerationError("generation_busy", retryable=True) from exc
        body = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0,
            "max_tokens": self._max_tokens,
            "reasoning_effort": "none",
            "include_reasoning": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "tool_choice": "none",
        }
        try:
            try:
                async with self._client.stream(
                    "POST",
                    "/v1/chat/completions",
                    json=body,
                    timeout=self._timeout,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        data = _sse_data(line)
                        if data is None or data == "[DONE]":
                            continue
                        payload = _json_object(data)
                        usage = _usage(payload.get("usage"))
                        if usage is not None:
                            yield GenerationChunk(usage=usage)
                        content = _content_delta(payload)
                        if content:
                            yield GenerationChunk(content=content)
            except httpx.HTTPStatusError as exc:
                retryable = exc.response.status_code >= 500 or exc.response.status_code == 429
                raise GenerationError("generation_upstream_error", retryable=retryable) from exc
            except httpx.TimeoutException as exc:
                raise GenerationError("generation_timeout", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise GenerationError("generation_unavailable", retryable=True) from exc
            except ValueError as exc:
                raise GenerationError("generation_protocol_error", retryable=True) from exc
        finally:
            self._semaphore.release()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _sse_data(line: str) -> str | None:
    if not line or line.startswith(":") or not line.startswith("data:"):
        return None
    return line[5:].lstrip()


def _json_object(value: str) -> dict[str, Any]:
    import orjson

    parsed = orjson.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("generation chunk is not an object")
    return parsed


def _content_delta(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        return ""
    content = delta.get("content")
    return content if isinstance(content, str) else ""


def _usage(value: object) -> UsageSummary | None:
    if not isinstance(value, dict):
        return None
    fields = (
        value.get("prompt_tokens"),
        value.get("completion_tokens"),
        value.get("total_tokens"),
    )
    if any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in fields):
        return None
    return UsageSummary(
        prompt_tokens=fields[0],
        completion_tokens=fields[1],
        total_tokens=fields[2],
    )
