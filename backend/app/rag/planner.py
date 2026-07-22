from __future__ import annotations

import re
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

MAX_QUERY_CHARS = 1_000
MAX_HINT_CHARS = 160
MAX_HINTS = 8
PLANNER_MAX_TOKENS = 400

QUOTED_TERM_RE = re.compile(r"[\"“”']([^\"“”']{1,160})[\"“”']")
IDENTIFIER_RE = re.compile(
    r"(?<!\w)(?:"
    r"(?=\w{3,40}(?!\w))(?=\w*\d)(?=\w*[^\W\d_])\w+"
    r"|(?=[\w./:-]{2,80}(?!\w))(?=[\w./:-]*\d)[\w]+(?:[./:_-][\w]+)+"
    r")(?!\w)",
    re.UNICODE,
)


class RetrievalPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    intent: Literal["smalltalk", "meta", "knowledge"]
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    exact_terms: tuple[str, ...] = Field(default_factory=tuple, max_length=MAX_HINTS)
    document_hints: tuple[str, ...] = Field(default_factory=tuple, max_length=MAX_HINTS)
    collection_hints: tuple[str, ...] = Field(default_factory=tuple, max_length=MAX_HINTS)
    heading_hints: tuple[str, ...] = Field(default_factory=tuple, max_length=MAX_HINTS)

    @field_validator("exact_terms", "document_hints", "collection_hints", "heading_hints")
    @classmethod
    def validate_hints(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            candidate = value.strip()
            if not candidate or len(candidate) > MAX_HINT_CHARS:
                raise ValueError(f"planner hints must contain 1-{MAX_HINT_CHARS} characters")
            key = candidate.casefold()
            if key not in seen:
                result.append(candidate)
                seen.add(key)
        return tuple(result)


class PlanningResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan: RetrievalPlan
    used_fallback: bool
    fallback_reason: (
        Literal["timeout", "http_error", "invalid_response", "planner_error"] | None
    ) = None


class Planner(Protocol):
    async def plan(self, message: str) -> PlanningResult: ...


class VllmPlanner:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_seconds: float = 15.0,
        max_tokens: int = PLANNER_MAX_TOKENS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._max_tokens = max_tokens
        self._client = client

    async def plan(self, message: str) -> PlanningResult:
        fallback = deterministic_fallback(message)
        try:
            payload = await self._request(message)
            content = _planner_content(payload)
            plan = RetrievalPlan.model_validate_json(content)
            return PlanningResult(plan=plan, used_fallback=False)
        except httpx.TimeoutException:
            return PlanningResult(plan=fallback, used_fallback=True, fallback_reason="timeout")
        except httpx.HTTPError:
            return PlanningResult(plan=fallback, used_fallback=True, fallback_reason="http_error")
        except (KeyError, TypeError, ValueError, ValidationError):
            return PlanningResult(
                plan=fallback,
                used_fallback=True,
                fallback_reason="invalid_response",
            )
        except Exception:
            return PlanningResult(
                plan=fallback, used_fallback=True, fallback_reason="planner_error"
            )

    async def _request(self, message: str) -> Any:
        body = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Plan retrieval for a multilingual private document corpus. "
                        "Return only the requested JSON. Preserve the user's language in query. "
                        "Hints are ranking suggestions, never authorization. Use knowledge unless "
                        "the message is clearly casual conversation or asks about the assistant."
                    ),
                },
                {"role": "user", "content": _bounded_message(message)},
            ],
            "temperature": 0,
            "max_tokens": self._max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "retrieval_plan",
                    "strict": True,
                    "schema": RetrievalPlan.model_json_schema(),
                },
            },
        }
        if self._client is not None:
            response = await self._client.post(
                "/v1/chat/completions", json=body, timeout=self._timeout
            )
        else:
            async with httpx.AsyncClient(base_url=self._base_url) as client:
                response = await client.post(
                    "/v1/chat/completions", json=body, timeout=self._timeout
                )
        response.raise_for_status()
        return response.json()


def deterministic_fallback(message: str) -> RetrievalPlan:
    query = _bounded_message(message)
    exact_terms: list[str] = []
    seen: set[str] = set()
    for pattern in (QUOTED_TERM_RE, IDENTIFIER_RE):
        for match in pattern.finditer(query):
            candidate = match.group(1) if match.lastindex else match.group(0)
            candidate = candidate.strip()[:MAX_HINT_CHARS]
            key = candidate.casefold()
            if candidate and key not in seen:
                exact_terms.append(candidate)
                seen.add(key)
            if len(exact_terms) == MAX_HINTS:
                break
        if len(exact_terms) == MAX_HINTS:
            break
    return RetrievalPlan(
        intent="knowledge",
        query=query,
        exact_terms=exact_terms,
        document_hints=[],
        collection_hints=[],
        heading_hints=[],
    )


def _bounded_message(message: str) -> str:
    candidate = message.strip()
    if not candidate:
        raise ValueError("message cannot be blank")
    return candidate[:MAX_QUERY_CHARS]


def _planner_content(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValueError("planner response must be an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("planner response must contain one choice")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ValueError("planner choice must be an object")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ValueError("planner choice has no message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("planner response has no content")
    return content
