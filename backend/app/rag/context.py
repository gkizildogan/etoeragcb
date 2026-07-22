from __future__ import annotations

from collections import Counter
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.rag.candidates import RerankedEvidence, stable_reranked_key


class TokenCounter(Protocol):
    async def count(self, text: str) -> int: ...


class TokenizerServiceError(RuntimeError):
    pass


class VllmTokenCounter:
    """Counts with the pinned generation model's serving tokenizer."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds
        )
        self._owns_client = client is None

    async def count(self, text: str) -> int:
        try:
            response = await self._client.post(
                "/tokenize",
                json={
                    "model": self._model,
                    "prompt": text,
                    "add_special_tokens": False,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TokenizerServiceError("vLLM tokenizer request failed") from exc
        if not isinstance(payload, dict):
            raise TokenizerServiceError("vLLM tokenizer returned an invalid payload")
        count = payload.get("count")
        tokens = payload.get("tokens")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            return count
        if isinstance(tokens, list):
            return len(tokens)
        raise TokenizerServiceError("vLLM tokenizer returned neither count nor tokens")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class ContextSkip(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    reason: Literal[
        "section_cap",
        "source_cap",
        "domain_cap",
        "source_type_cap",
        "candidate_cap",
        "token_budget",
    ]


class PackedSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(pattern=r"^S[1-9][0-9]*$")
    evidence: RerankedEvidence


class PackedContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    token_count: int = Field(ge=0)
    token_budget: int = Field(ge=1)
    sources: tuple[PackedSource, ...]
    skipped: tuple[ContextSkip, ...]


class ContextPacker:
    def __init__(
        self,
        token_counter: TokenCounter,
        *,
        token_budget: int,
        max_candidates: int,
        section_limit: int,
        source_limit: int,
        domain_limit: int,
        web_limit: int | None = None,
    ) -> None:
        limits = (token_budget, max_candidates, section_limit, source_limit, domain_limit)
        if any(limit < 1 for limit in limits):
            raise ValueError("all context limits must be positive")
        if web_limit is not None and (web_limit < 1 or web_limit > max_candidates):
            raise ValueError("web context limit must be within the candidate limit")
        self._counter = token_counter
        self._token_budget = token_budget
        self._max_candidates = max_candidates
        self._section_limit = section_limit
        self._source_limit = source_limit
        self._domain_limit = domain_limit
        self._web_limit = web_limit

    async def pack(self, candidates: tuple[RerankedEvidence, ...]) -> PackedContext:
        ordered = _evidence_order(candidates)
        selected: list[RerankedEvidence] = []
        skipped: list[ContextSkip] = []
        section_counts: Counter[str] = Counter()
        source_counts: Counter[str] = Counter()
        domain_counts: Counter[str] = Counter()
        source_type_counts: Counter[str] = Counter()
        context = ""
        token_count = 0
        for item in ordered:
            candidate = item.candidate
            if len(selected) >= self._max_candidates:
                skipped.append(
                    ContextSkip(candidate_id=candidate.candidate_id, reason="candidate_cap")
                )
                continue
            if section_counts[candidate.section_key] >= self._section_limit:
                skipped.append(
                    ContextSkip(candidate_id=candidate.candidate_id, reason="section_cap")
                )
                continue
            if source_counts[candidate.source_key] >= self._source_limit:
                skipped.append(
                    ContextSkip(candidate_id=candidate.candidate_id, reason="source_cap")
                )
                continue
            if (
                candidate.domain is not None
                and domain_counts[candidate.domain] >= self._domain_limit
            ):
                skipped.append(
                    ContextSkip(candidate_id=candidate.candidate_id, reason="domain_cap")
                )
                continue
            if (
                candidate.source_type == "web"
                and self._web_limit is not None
                and source_type_counts["web"] >= self._web_limit
            ):
                skipped.append(
                    ContextSkip(candidate_id=candidate.candidate_id, reason="source_type_cap")
                )
                continue
            tentative_items = [*selected, item]
            tentative = _format_context(tentative_items)
            tentative_count = await self._counter.count(tentative)
            if tentative_count > self._token_budget:
                skipped.append(
                    ContextSkip(candidate_id=candidate.candidate_id, reason="token_budget")
                )
                continue
            selected.append(item)
            section_counts[candidate.section_key] += 1
            source_counts[candidate.source_key] += 1
            if candidate.domain is not None:
                domain_counts[candidate.domain] += 1
            source_type_counts[candidate.source_type] += 1
            context = tentative
            token_count = tentative_count
        sources = tuple(
            PackedSource(source_id=f"S{index}", evidence=item)
            for index, item in enumerate(selected, start=1)
        )
        return PackedContext(
            text=context,
            token_count=token_count,
            token_budget=self._token_budget,
            sources=sources,
            skipped=tuple(skipped),
        )


def _evidence_order(candidates: tuple[RerankedEvidence, ...]) -> tuple[RerankedEvidence, ...]:
    ranked = sorted(candidates, key=stable_reranked_key)
    representatives: list[RerankedEvidence] = []
    represented_ids: set[str] = set()
    represented_terms: set[str] = set()
    for item in ranked:
        new_terms = set(item.candidate.matched_exact_terms) - represented_terms
        if not new_terms:
            continue
        representatives.append(item)
        represented_ids.add(item.candidate.candidate_id)
        represented_terms.update(item.candidate.matched_exact_terms)
    for source_type in ("document", "web"):
        representative = next(
            (
                item
                for item in ranked
                if item.candidate.source_type == source_type
                and item.candidate.candidate_id not in represented_ids
            ),
            None,
        )
        if representative is not None:
            representatives.append(representative)
            represented_ids.add(representative.candidate.candidate_id)
    return tuple(
        [
            *representatives,
            *(item for item in ranked if item.candidate.candidate_id not in represented_ids),
        ]
    )


def _format_context(candidates: list[RerankedEvidence]) -> str:
    blocks: list[str] = []
    for index, item in enumerate(candidates, start=1):
        candidate = item.candidate
        metadata = [
            f"type={candidate.source_type}",
            f"title={candidate.title}",
        ]
        if candidate.source_filename is not None:
            metadata.append(f"file={candidate.source_filename}")
        if candidate.page_start is not None and candidate.page_end is not None:
            metadata.append(f"pages={candidate.page_start}-{candidate.page_end}")
        if candidate.uri is not None:
            metadata.append(f"url={candidate.uri}")
        if candidate.source_type == "web":
            content = (
                f"--- BEGIN UNTRUSTED WEB SOURCE S{index} ---\n"
                f"{candidate.text_original}\n"
                f"--- END UNTRUSTED WEB SOURCE S{index} ---"
            )
        else:
            content = candidate.text_original
        blocks.append(f"[S{index}] {' | '.join(metadata)}\n{content}")
    return "\n\n".join(blocks)
