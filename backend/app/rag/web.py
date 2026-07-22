from __future__ import annotations

import asyncio
import hashlib
import math
from typing import Any, Literal, Protocol

import httpx
import orjson
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.ingest.normalization import normalize_lexical
from app.rag.candidates import EvidenceCandidate
from app.web.extract import extract_text
from app.web.fetcher import FetchedPage

SEARCH_RESPONSE_LIMIT = 1_000_000


class WebRetrievalError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rank: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=1024)
    url: str = Field(min_length=1, max_length=4096)
    snippet: str = Field(default="", max_length=4000)
    score: float
    engines: tuple[str, ...] = ()


class PageFetcher(Protocol):
    async def fetch(self, url: str) -> FetchedPage: ...


class SearchProvider(Protocol):
    async def search(self, query: str) -> tuple[SearchHit, ...]: ...


class WebFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    search_rank: int | None = Field(default=None, ge=1)
    code: str = Field(min_length=1, max_length=80)


class WebRetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["disabled", "empty", "ok", "partial", "failed"]
    candidates: tuple[EvidenceCandidate, ...] = ()
    failures: tuple[WebFailure, ...] = ()


class SearxngClient:
    def __init__(
        self,
        base_url: str,
        *,
        result_limit: int,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._result_limit = result_limit
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds
        )
        self._owns_client = client is None

    async def search(self, query: str) -> tuple[SearchHit, ...]:
        bounded_query = query.strip()[:2000]
        if not bounded_query:
            return ()
        try:
            async with self._client.stream(
                "GET",
                "/search",
                params={
                    "q": bounded_query,
                    "format": "json",
                    "categories": "general",
                    "safesearch": "1",
                },
            ) as response:
                response.raise_for_status()
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > SEARCH_RESPONSE_LIMIT:
                        raise WebRetrievalError(
                            "search_response_too_large", "SearXNG response exceeded its limit"
                        )
            payload: Any = orjson.loads(raw)
        except WebRetrievalError:
            raise
        except (httpx.HTTPError, orjson.JSONDecodeError) as exc:
            raise WebRetrievalError("search_failure", "SearXNG request failed") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise WebRetrievalError("search_invalid", "SearXNG returned an invalid payload")
        hits: list[SearchHit] = []
        seen_urls: set[str] = set()
        for item in payload["results"]:
            if len(hits) >= self._result_limit:
                break
            parsed = _parse_search_hit(item, len(hits) + 1)
            if parsed is None or parsed.url in seen_urls:
                continue
            seen_urls.add(parsed.url)
            hits.append(parsed)
        return tuple(hits)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class InternalPageFetcher:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds + 2
        )
        self._owns_client = client is None

    async def fetch(self, url: str) -> FetchedPage:
        try:
            response = await self._client.post("/fetch", json={"url": url})
        except httpx.HTTPError as exc:
            raise WebRetrievalError("fetcher_unavailable", "page fetcher request failed") from exc
        if response.status_code != 200:
            code = "page_rejected"
            try:
                payload = response.json()
                if isinstance(payload, dict) and isinstance(payload.get("code"), str):
                    code = payload["code"][:80]
            except ValueError:
                pass
            raise WebRetrievalError(code, "page fetcher rejected the URL")
        try:
            return FetchedPage.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise WebRetrievalError(
                "fetcher_invalid", "page fetcher returned invalid data"
            ) from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class WebRetriever:
    def __init__(
        self,
        search: SearchProvider,
        fetcher: PageFetcher,
        *,
        concurrency: int,
    ) -> None:
        if concurrency < 1:
            raise ValueError("web fetch concurrency must be positive")
        self._search = search
        self._fetcher = fetcher
        self._concurrency = concurrency

    async def retrieve(self, query: str) -> WebRetrievalResult:
        try:
            hits = await self._search.search(query)
        except WebRetrievalError as exc:
            return WebRetrievalResult(status="failed", failures=(WebFailure(code=exc.code),))
        except Exception:
            return WebRetrievalResult(
                status="failed", failures=(WebFailure(code="search_unexpected"),)
            )
        if not hits:
            return WebRetrievalResult(status="empty")
        semaphore = asyncio.Semaphore(self._concurrency)

        async def fetch(hit: SearchHit) -> tuple[SearchHit, FetchedPage | None, WebFailure | None]:
            try:
                async with semaphore:
                    page = await self._fetcher.fetch(hit.url)
                return hit, page, None
            except WebRetrievalError as exc:
                return hit, None, WebFailure(search_rank=hit.rank, code=exc.code)
            except Exception:
                return hit, None, WebFailure(search_rank=hit.rank, code="fetch_unexpected")

        fetched = await asyncio.gather(*(fetch(hit) for hit in hits))
        candidates: list[EvidenceCandidate] = []
        failures: list[WebFailure] = []
        final_urls: set[str] = set()
        for hit, page, failure in sorted(fetched, key=lambda item: item[0].rank):
            if failure is not None:
                failures.append(failure)
                continue
            assert page is not None
            if page.final_url in final_urls:
                failures.append(WebFailure(search_rank=hit.rank, code="duplicate_final_url"))
                continue
            final_urls.add(page.final_url)
            candidates.append(_web_candidate(hit, page))
        if candidates and failures:
            result_status = "partial"
        elif candidates:
            result_status = "ok"
        elif failures:
            result_status = "failed"
        else:
            result_status = "empty"
        return WebRetrievalResult(
            status=result_status,
            candidates=tuple(candidates),
            failures=tuple(failures),
        )


def _parse_search_hit(value: object, rank: int) -> SearchHit | None:
    if not isinstance(value, dict):
        return None
    url = value.get("url")
    raw_title = value.get("title")
    if not isinstance(url, str) or not isinstance(raw_title, str):
        return None
    title = _search_text(raw_title, 1024)
    if not title:
        title = "Untitled web result"
    raw_snippet = value.get("content")
    snippet = _search_text(raw_snippet if isinstance(raw_snippet, str) else "", 4000)
    raw_score = value.get("score")
    score = (
        float(raw_score)
        if isinstance(raw_score, int | float)
        and not isinstance(raw_score, bool)
        and math.isfinite(float(raw_score))
        else 1 / rank
    )
    raw_engines = value.get("engines")
    engines = (
        tuple(str(item)[:80] for item in raw_engines[:10] if isinstance(item, str))
        if isinstance(raw_engines, list)
        else ()
    )
    try:
        return SearchHit(
            rank=rank,
            title=title,
            url=url,
            snippet=snippet,
            score=score,
            engines=engines,
        )
    except ValidationError:
        return None


def _search_text(value: str, limit: int) -> str:
    if not value:
        return ""
    try:
        text, _media = extract_text(value.encode("utf-8"), "text/html; charset=utf-8", limit)
    except Exception:
        return ""
    return text[:limit]


def _web_candidate(hit: SearchHit, page: FetchedPage) -> EvidenceCandidate:
    lexical = normalize_lexical(page.text)
    content_hash = hashlib.sha256(page.text.encode()).hexdigest()
    lexical_hash = hashlib.sha256(lexical.encode()).hexdigest()
    candidate_id = "web-" + hashlib.sha256(f"{page.final_url}\0{content_hash}".encode()).hexdigest()
    return EvidenceCandidate(
        candidate_id=candidate_id,
        source_type="web",
        source_key=page.final_url,
        section_key=f"{page.final_url}#main",
        domain=page.domain,
        title=hit.title,
        uri=page.final_url,
        text_original=page.text,
        text_lexical=lexical,
        content_sha256=content_hash,
        lexical_sha256=lexical_hash,
        retrieval_rank=hit.rank,
        retrieval_score=hit.score,
        provenance={
            "untrusted_source": True,
            "search_rank": hit.rank,
            "search_engines": list(hit.engines),
            "search_snippet": hit.snippet,
            "content_type": page.content_type,
            "bytes_received": page.bytes_received,
            "redirect_count": page.redirect_count,
        },
    )
