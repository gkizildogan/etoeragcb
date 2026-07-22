from __future__ import annotations

from typing import Any

import httpx

from app.ingest.chunker import TokenSpan


class EmbeddingError(RuntimeError):
    pass


class TokenizationTooLongError(EmbeddingError):
    pass


class TeiClient:
    def __init__(
        self,
        base_url: str,
        *,
        expected_dimension: int,
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._expected_dimension = expected_dimension
        self._timeout = timeout_seconds
        self._client = client

    async def token_spans(self, text: str) -> list[TokenSpan]:
        return await self._token_spans(text, base_offset=0)

    async def _token_spans(self, text: str, *, base_offset: int) -> list[TokenSpan]:
        try:
            payload = await self._post("/tokenize", {"inputs": text, "add_special_tokens": False})
        except TokenizationTooLongError:
            if len(text) < 2:
                raise
            split = _safe_split(text)
            left = await self._token_spans(text[:split], base_offset=base_offset)
            right = await self._token_spans(text[split:], base_offset=base_offset + split)
            return [*left, *right]
        tokens: Any = payload
        if isinstance(payload, dict):
            tokens = payload.get("tokens", payload.get("data"))
        if isinstance(tokens, list) and len(tokens) == 1 and isinstance(tokens[0], list):
            tokens = tokens[0]
        if not isinstance(tokens, list):
            raise EmbeddingError("TEI tokenize response has an unexpected shape")
        byte_to_character = _utf8_boundary_map(text)
        spans: list[TokenSpan] = []
        for token in tokens:
            if not isinstance(token, dict) or token.get("special") is True:
                continue
            start = token.get("start")
            end = token.get("stop", token.get("end"))
            if isinstance(start, int) and isinstance(end, int):
                character_start = byte_to_character.get(start)
                character_end = byte_to_character.get(end)
                if character_start is None or character_end is None:
                    raise EmbeddingError("TEI returned a non-boundary UTF-8 token offset")
                spans.append(
                    TokenSpan(
                        start=base_offset + character_start,
                        end=base_offset + character_end,
                    )
                )
        if not spans and text.strip():
            raise EmbeddingError("TEI tokenize response did not include source offsets")
        return spans

    async def embed(self, texts: list[str]) -> list[list[float]]:
        payload = await self._post("/embed", {"inputs": texts, "truncate": False})
        if not isinstance(payload, list) or len(payload) != len(texts):
            raise EmbeddingError("TEI embedding count does not match the request")
        embeddings: list[list[float]] = []
        for vector in payload:
            if not isinstance(vector, list) or len(vector) != self._expected_dimension:
                raise EmbeddingError("TEI embedding dimension does not match EMBED_DIM")
            embeddings.append([float(value) for value in vector])
        return embeddings

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        try:
            if self._client is not None:
                response = await self._client.post(path, json=body, timeout=self._timeout)
            else:
                async with httpx.AsyncClient(base_url=self._base_url) as client:
                    response = await client.post(path, json=body, timeout=self._timeout)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if path == "/tokenize" and exc.response.status_code in {413, 422}:
                raise TokenizationTooLongError("TEI token limit exceeded") from exc
            raise EmbeddingError(f"TEI request failed for {path}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingError(f"TEI request failed for {path}") from exc


def _utf8_boundary_map(text: str) -> dict[int, int]:
    result = {0: 0}
    byte_offset = 0
    for character_offset, character in enumerate(text, start=1):
        byte_offset += len(character.encode("utf-8"))
        result[byte_offset] = character_offset
    return result


def _safe_split(text: str) -> int:
    midpoint = len(text) // 2
    before = text.rfind(" ", 0, midpoint)
    after = text.find(" ", midpoint)
    candidates = [position for position in (before, after) if 0 < position < len(text)]
    if not candidates:
        return midpoint
    boundary = min(candidates, key=lambda position: abs(position - midpoint))
    return boundary + 1
