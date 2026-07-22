from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from collections.abc import Iterable
from difflib import SequenceMatcher
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.normalization import normalize_lexical
from app.models import (
    Document,
    DocumentCollection,
    IndexGenerationDocument,
    KnowledgeCollection,
    Section,
    Tenant,
)
from app.rag.planner import RetrievalPlan

FUZZY_HINT_MINIMUM = 0.82
logger = structlog.get_logger(__name__)

HintKind = Literal["document", "collection", "heading"]
ResolutionKind = Literal[
    "explicit_scope", "exact_scope", "ambiguous_boost", "fuzzy_boost", "no_match"
]


class ScopeValidationError(LookupError):
    """An explicit scope does not exist in the tenant's active corpus."""


class HintDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: HintKind
    hint: str
    resolution: ResolutionKind
    matched_ids: tuple[uuid.UUID, ...] = ()
    expanded_section_count: int = 0


class HintBoost(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hint: str
    target: Literal["document", "section"]
    target_ids: tuple[uuid.UUID, ...]


class ResolvedScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: uuid.UUID
    generation_id: int | None
    retrieval_revision: int
    document_ids: tuple[uuid.UUID, ...]
    version_ids: tuple[uuid.UUID, ...]
    section_ids: tuple[uuid.UUID, ...] | None = None
    decisions: tuple[HintDecision, ...] = ()
    boosts: tuple[HintBoost, ...] = ()


class MetadataResolver:
    async def resolve(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        plan: RetrievalPlan,
        explicit_document_ids: tuple[uuid.UUID, ...] = (),
        explicit_collection_ids: tuple[uuid.UUID, ...] = (),
    ) -> ResolvedScope:
        tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        if tenant is None:
            raise ScopeValidationError("scope not found")
        generation_id = tenant.active_index_generation_id
        manifest_rows = []
        if generation_id is not None:
            manifest_rows = list(
                (
                    await session.execute(
                        select(
                            IndexGenerationDocument.document_id,
                            IndexGenerationDocument.document_version_id,
                            Document.title,
                            Document.source_filename,
                        )
                        .join(
                            Document,
                            (Document.id == IndexGenerationDocument.document_id)
                            & (Document.tenant_id == IndexGenerationDocument.tenant_id),
                        )
                        .where(
                            IndexGenerationDocument.generation_id == generation_id,
                            IndexGenerationDocument.tenant_id == tenant_id,
                            Document.deleted_at.is_(None),
                        )
                    )
                ).all()
            )
        documents = {
            row.document_id: (row.document_version_id, row.title, row.source_filename)
            for row in manifest_rows
        }
        explicit_documents = set(explicit_document_ids)
        if explicit_documents - documents.keys():
            raise ScopeValidationError("scope not found")

        collections = list(
            await session.scalars(
                select(KnowledgeCollection).where(
                    KnowledgeCollection.tenant_id == tenant_id,
                    KnowledgeCollection.deleted_at.is_(None),
                )
            )
        )
        collections_by_id = {item.id: item for item in collections}
        explicit_collections = set(explicit_collection_ids)
        if explicit_collections - collections_by_id.keys():
            raise ScopeValidationError("scope not found")

        memberships: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        if documents:
            rows = (
                await session.execute(
                    select(
                        DocumentCollection.document_id,
                        DocumentCollection.collection_id,
                    ).where(
                        DocumentCollection.tenant_id == tenant_id,
                        DocumentCollection.document_id.in_(documents),
                        DocumentCollection.collection_id.in_(collections_by_id),
                    )
                )
            ).all()
            for document_id, collection_id in rows:
                memberships[collection_id].add(document_id)

        allowed_documents = set(documents)
        decisions: list[HintDecision] = []
        boosts: list[HintBoost] = []
        if explicit_documents:
            allowed_documents &= explicit_documents
            decisions.append(
                HintDecision(
                    kind="document",
                    hint="explicit",
                    resolution="explicit_scope",
                    matched_ids=_sorted_uuids(explicit_documents),
                )
            )
        if explicit_collections:
            collection_documents = set().union(
                *(memberships.get(item, set()) for item in explicit_collections)
            )
            allowed_documents &= collection_documents
            decisions.append(
                HintDecision(
                    kind="collection",
                    hint="explicit",
                    resolution="explicit_scope",
                    matched_ids=_sorted_uuids(explicit_collections),
                )
            )

        document_scope: set[uuid.UUID] = set()
        for hint in plan.document_hints:
            matched, resolution = _resolve_document_hint(hint, allowed_documents, documents)
            decision = HintDecision(
                kind="document",
                hint=hint,
                resolution=resolution,
                matched_ids=_sorted_uuids(matched),
            )
            decisions.append(decision)
            _log_decision(decision)
            if resolution == "exact_scope":
                document_scope.update(matched)
            elif resolution in {"ambiguous_boost", "fuzzy_boost"} and matched:
                boosts.append(
                    HintBoost(
                        hint=hint,
                        target="document",
                        target_ids=_sorted_uuids(matched),
                    )
                )
        if document_scope:
            allowed_documents &= document_scope

        collection_scope: set[uuid.UUID] = set()
        for hint in plan.collection_hints:
            matched, resolution = _resolve_collection_hint(hint, collections)
            decision = HintDecision(
                kind="collection",
                hint=hint,
                resolution=resolution,
                matched_ids=_sorted_uuids(matched),
            )
            decisions.append(decision)
            _log_decision(decision)
            matched_documents = set().union(*(memberships.get(item, set()) for item in matched))
            if resolution == "exact_scope":
                collection_scope.update(matched_documents)
            elif resolution in {"ambiguous_boost", "fuzzy_boost"} and matched_documents:
                boosts.append(
                    HintBoost(
                        hint=hint,
                        target="document",
                        target_ids=_sorted_uuids(matched_documents),
                    )
                )
        if any(item.resolution == "exact_scope" for item in decisions if item.kind == "collection"):
            allowed_documents &= collection_scope

        document_ids = _sorted_uuids(allowed_documents)
        version_ids = _sorted_uuids(documents[item][0] for item in allowed_documents)
        sections = []
        if version_ids:
            sections = list(
                await session.scalars(
                    select(Section).where(
                        Section.tenant_id == tenant_id,
                        Section.document_version_id.in_(version_ids),
                    )
                )
            )

        hard_sections: set[uuid.UUID] = set()
        for hint in plan.heading_hints:
            roots, resolution = _resolve_heading_hint(hint, sections)
            expanded = _expand_section_descendants(roots, sections)
            decision = HintDecision(
                kind="heading",
                hint=hint,
                resolution=resolution,
                matched_ids=_sorted_uuids(roots),
                expanded_section_count=len(expanded),
            )
            decisions.append(decision)
            _log_decision(decision)
            if resolution == "exact_scope":
                hard_sections.update(expanded)
            elif resolution in {"ambiguous_boost", "fuzzy_boost"} and expanded:
                boosts.append(
                    HintBoost(
                        hint=hint,
                        target="section",
                        target_ids=_sorted_uuids(expanded),
                    )
                )

        return ResolvedScope(
            tenant_id=tenant_id,
            generation_id=generation_id,
            retrieval_revision=tenant.retrieval_revision,
            document_ids=document_ids,
            version_ids=version_ids,
            section_ids=_sorted_uuids(hard_sections) if hard_sections else None,
            decisions=tuple(decisions),
            boosts=tuple(boosts),
        )


def _resolve_document_hint(
    hint: str,
    allowed: set[uuid.UUID],
    documents: dict[uuid.UUID, tuple[uuid.UUID, str, str]],
) -> tuple[set[uuid.UUID], ResolutionKind]:
    identifier = _uuid_or_none(hint)
    if identifier is not None and identifier in allowed:
        return {identifier}, "exact_scope"
    normalized = normalize_lexical(hint)
    exact = {
        document_id
        for document_id in allowed
        if normalized
        in {
            normalize_lexical(documents[document_id][1]),
            normalize_lexical(documents[document_id][2]),
        }
    }
    if len(exact) == 1:
        return exact, "exact_scope"
    if exact:
        return exact, "ambiguous_boost"
    fuzzy = _fuzzy_matches(
        normalized,
        {
            document_id: (
                normalize_lexical(documents[document_id][1]),
                normalize_lexical(documents[document_id][2]),
            )
            for document_id in allowed
        },
    )
    return (fuzzy, "fuzzy_boost") if fuzzy else (set(), "no_match")


def _resolve_collection_hint(
    hint: str, collections: list[KnowledgeCollection]
) -> tuple[set[uuid.UUID], ResolutionKind]:
    identifier = _uuid_or_none(hint)
    if identifier is not None and any(item.id == identifier for item in collections):
        return {identifier}, "exact_scope"
    normalized = normalize_lexical(hint)
    exact = {item.id for item in collections if normalize_lexical(item.name) == normalized}
    if len(exact) == 1:
        return exact, "exact_scope"
    if exact:
        return exact, "ambiguous_boost"
    fuzzy = _fuzzy_matches(
        normalized, {item.id: (normalize_lexical(item.name),) for item in collections}
    )
    return (fuzzy, "fuzzy_boost") if fuzzy else (set(), "no_match")


def _resolve_heading_hint(
    hint: str, sections: list[Section]
) -> tuple[set[uuid.UUID], ResolutionKind]:
    identifier = _uuid_or_none(hint)
    if identifier is not None and any(item.id == identifier for item in sections):
        return {identifier}, "exact_scope"
    normalized = normalize_lexical(hint)
    path_exact = {item.id for item in sections if item.path_lexical == normalized}
    if len(path_exact) == 1:
        return path_exact, "exact_scope"
    heading_exact = {item.id for item in sections if item.heading_lexical == normalized}
    exact = path_exact or heading_exact
    if len(exact) == 1:
        return exact, "exact_scope"
    if exact:
        return exact, "ambiguous_boost"
    fuzzy = _fuzzy_matches(
        normalized,
        {item.id: (item.heading_lexical, item.path_lexical) for item in sections},
    )
    return (fuzzy, "fuzzy_boost") if fuzzy else (set(), "no_match")


def _expand_section_descendants(roots: set[uuid.UUID], sections: list[Section]) -> set[uuid.UUID]:
    root_sections = [item for item in sections if item.id in roots]
    expanded: set[uuid.UUID] = set()
    for root in root_sections:
        prefix = f"{root.path_lexical} / "
        expanded.update(
            item.id
            for item in sections
            if item.document_version_id == root.document_version_id
            and (item.path_lexical == root.path_lexical or item.path_lexical.startswith(prefix))
        )
    return expanded


def _fuzzy_matches(hint: str, values: dict[uuid.UUID, tuple[str, ...]]) -> set[uuid.UUID]:
    if not hint:
        return set()
    scores = {
        item_id: max(SequenceMatcher(None, hint, candidate).ratio() for candidate in candidates)
        for item_id, candidates in values.items()
        if any(candidates)
    }
    if not scores:
        return set()
    best = max(scores.values())
    if best < FUZZY_HINT_MINIMUM:
        return set()
    return {item_id for item_id, score in scores.items() if score == best}


def _uuid_or_none(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _sorted_uuids(values: Iterable[uuid.UUID]) -> tuple[uuid.UUID, ...]:
    return tuple(sorted(values, key=str))


def _log_decision(decision: HintDecision) -> None:
    logger.info(
        "retrieval_hint_resolved",
        hint_kind=decision.kind,
        hint_sha256=hashlib.sha256(decision.hint.encode()).hexdigest()[:12],
        resolution=decision.resolution,
        match_count=len(decision.matched_ids),
        expanded_section_count=decision.expanded_section_count,
    )
