from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from app.rag.candidates import EvidenceCandidate, RerankedEvidence
from app.rag.context import ContextPacker, VllmTokenCounter
from app.rag.dedup import deduplicate
from app.rag.gate import (
    ConfidenceGate,
    DatasetProvenance,
    GateArtifact,
    GateConfiguration,
    GateThresholds,
    ModelProvenance,
    load_gate_artifact,
)
from app.rag.reranker import TeiReranker

EMBEDDING_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
RERANKER_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"


class CharacterTokenCounter:
    async def count(self, text: str) -> int:
        return len(text)


async def test_tei_reranker_batches_orders_and_caches_scores() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        results = [
            {"index": index, "score": 0.95 if text == "doğru yan\u0131t" else 0.1}
            for index, text in enumerate(body["texts"])
        ]
        return httpx.Response(200, json=list(reversed(results)), request=request)

    candidates = tuple(
        _evidence(
            f"item-{index}",
            "doğru yan\u0131t" if index == 37 else f"irrelevant {index}",
            rank=index + 1,
        )
        for index in range(40)
    )
    cache = MemoryCache()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://tei"
    ) as client:
        reranker = TeiReranker(
            "http://tei",
            model_revision=RERANKER_REVISION,
            max_candidates=50,
            batch_size=32,
            cache=cache,
            cache_ttl=300,
            client=client,
        )
        first = await reranker.rerank("hangi yan\u0131t doğru?", candidates)
        second = await reranker.rerank("hangi yan\u0131t doğru?", candidates)

    assert [len(request["texts"]) for request in requests] == [32, 8]
    assert all(request["raw_scores"] is False for request in requests)
    assert first[0].candidate.candidate_id == "item-37"
    assert first == second


async def test_vllm_token_counter_uses_generation_serving_protocol() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"tokens": [10, 11, 12], "count": 3}, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://vllm"
    ) as client:
        counter = VllmTokenCounter("http://vllm", "fixture-model", client=client)
        count = await counter.count("Türkçe and English")

    assert count == 3
    assert bodies == [
        {
            "model": "fixture-model",
            "prompt": "Türkçe and English",
            "add_special_tokens": False,
        }
    ]


def test_dedup_removes_hash_overlap_and_near_duplicates_but_preserves_exact_terms() -> None:
    base = _reranked(_evidence("base", "one two three four five six", rank=1), rank=1)
    same_content = _reranked(
        _evidence("same-content", "one two three four five six", rank=2), rank=2
    )
    overlap = _reranked(
        _evidence(
            "overlap",
            "separate overlapping window",
            rank=3,
            source="doc-overlap",
            section="section-overlap",
            char_start=10,
            char_end=90,
        ),
        rank=3,
    )
    overlap_root = _reranked(
        _evidence(
            "overlap-root",
            "root overlapping window",
            rank=2,
            source="doc-overlap",
            section="section-overlap",
            char_start=0,
            char_end=100,
        ),
        rank=2,
    )
    long_text = " ".join(f"token-{index}" for index in range(20))
    near_text = f"{' '.join(f'token-{index}' for index in range(19))} changed"
    near_root = _reranked(
        _evidence(
            "near-root",
            long_text,
            rank=4,
            source="doc-near",
            section="section-near",
            char_start=0,
            char_end=200,
        ),
        rank=4,
    )
    near = _reranked(
        _evidence(
            "near",
            near_text,
            rank=5,
            source="doc-near",
            section="section-near",
            char_start=300,
            char_end=500,
        ),
        rank=5,
    )
    protected = _reranked(
        _evidence(
            "protected",
            near_text,
            rank=6,
            source="doc-near",
            section="section-near",
            char_start=600,
            char_end=800,
            exact_terms=("ZX-42",),
            hash_salt="protected",
        ),
        rank=6,
    )

    result = deduplicate((same_content, overlap, near, protected, base, overlap_root, near_root))

    kept_ids = {item.candidate.candidate_id for item in result.candidates}
    reasons = {decision.reason for decision in result.decisions}
    assert "same-content" not in kept_ids
    assert "overlap" not in kept_ids
    assert "near" not in kept_ids
    assert "protected" in kept_ids
    assert {"content_hash", "overlapping_span", "near_duplicate"} <= reasons
    assert any("ZX-42" in item.candidate.matched_exact_terms for item in result.candidates)


async def test_context_selection_preserves_exact_hit_and_enforces_source_domain_caps() -> None:
    candidates = (
        _reranked(_web_evidence("web-a-1", rank=1, domain="a.example"), rank=1),
        _reranked(_web_evidence("web-a-2", rank=2, domain="a.example"), rank=2),
        _reranked(_web_evidence("web-a-3", rank=3, domain="a.example"), rank=3),
        _reranked(_evidence("doc-1", "general document evidence", rank=4), rank=4),
        _reranked(
            _evidence("exact", "ZX-42 identifier evidence", rank=5, exact_terms=("ZX-42",)),
            rank=5,
        ),
    )
    packer = ContextPacker(
        CharacterTokenCounter(),
        token_budget=2000,
        max_candidates=5,
        section_limit=2,
        source_limit=2,
        domain_limit=2,
    )

    packed = await packer.pack(candidates)

    selected_ids = [source.evidence.candidate.candidate_id for source in packed.sources]
    domains = Counter(
        source.evidence.candidate.domain
        for source in packed.sources
        if source.evidence.candidate.domain is not None
    )
    assert selected_ids[0] == "exact"
    assert "exact" in selected_ids
    assert max(domains.values(), default=0) <= 2
    assert any(skip.reason == "domain_cap" for skip in packed.skipped)
    assert packed.token_count <= packed.token_budget


async def test_context_budget_and_selection_are_deterministic_across_generated_corpora() -> None:
    for seed in range(30):
        randomizer = random.Random(seed)  # noqa: S311 - deterministic property fixture
        candidates = tuple(
            _reranked(
                _evidence(
                    f"candidate-{index}",
                    "x" * randomizer.randint(10, 90),
                    rank=index + 1,
                    source=f"doc-{index % 5}",
                    section=f"section-{index % 9}",
                    exact_terms=("ID-7",) if index == 7 else (),
                ),
                rank=index + 1,
                score=1.0 - index / 100,
            )
            for index in range(30)
        )
        packer = ContextPacker(
            CharacterTokenCounter(),
            token_budget=randomizer.randint(350, 900),
            max_candidates=12,
            section_limit=2,
            source_limit=3,
            domain_limit=2,
        )
        first = await packer.pack(candidates)
        shuffled = list(candidates)
        randomizer.shuffle(shuffled)
        second = await packer.pack(tuple(shuffled))

        assert first == second
        assert first.token_count == len(first.text)
        assert first.token_count <= first.token_budget
        section_counts = Counter(source.evidence.candidate.section_key for source in first.sources)
        document_counts = Counter(source.evidence.candidate.source_key for source in first.sources)
        assert max(section_counts.values(), default=0) <= 2
        assert max(document_counts.values(), default=0) <= 3
        assert len(first.sources) <= 12


def test_gate_fails_closed_without_calibration_and_for_empty_context() -> None:
    artifact = load_gate_artifact(Path("app/rag/calibration/retrieval_gate.v1.json"))
    gate = ConfidenceGate(
        artifact,
        embedding_model="BAAI/bge-m3",
        embedding_revision=EMBEDDING_REVISION,
        reranker_model="BAAI/bge-reranker-v2-m3",
        reranker_revision=RERANKER_REVISION,
    )
    candidate = _reranked(_evidence("candidate", "answer", rank=1), rank=1, score=0.99)

    unavailable = gate.evaluate((candidate,))
    empty = gate.evaluate(())

    assert unavailable.route == "no_answer"
    assert unavailable.reasons == ("calibration_unavailable",)
    assert unavailable.scores.top_score == 0.99
    assert empty.route == "no_answer" and empty.reasons == ("no_candidates",)
    assert unavailable.artifact_sha256 == artifact.sha256


def test_representative_labeled_gate_cases_are_auditable() -> None:
    gate = _calibrated_gate()
    confident = (
        _reranked(_evidence("high", "relevant", rank=1), rank=1, score=0.9),
        _reranked(_evidence("support", "support", rank=2), rank=2, score=0.7),
    )
    ambiguous = (
        _reranked(_evidence("ambiguous-a", "maybe", rank=1), rank=1, score=0.8),
        _reranked(_evidence("ambiguous-b", "maybe too", rank=2), rank=2, score=0.75),
    )
    weak = (
        _reranked(_evidence("weak-a", "weak", rank=1), rank=1, score=0.6),
        _reranked(_evidence("weak-b", "weak", rank=2), rank=2, score=0.4),
    )
    exact = (
        _reranked(
            _evidence("exact", "ZX-42", rank=1, exact_terms=("ZX-42",)),
            rank=1,
            score=0.86,
        ),
    )

    assert gate.evaluate(confident).route == "answer"
    assert gate.evaluate(ambiguous).reasons == ("score_margin_below_threshold",)
    assert gate.evaluate(weak).reasons == ("top_score_below_threshold",)
    exact_decision = gate.evaluate(exact)
    assert exact_decision.route == "answer"
    assert exact_decision.reasons == ("exact_threshold_passed",)
    assert exact_decision.dataset_name == "p6-synthetic-gate-cases"
    mismatch = _calibrated_gate(running_reranker_revision="0" * 40).evaluate(confident)
    assert mismatch.route == "no_answer"
    assert mismatch.reasons == ("model_revision_mismatch",)


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    async def get_json(self, key: str) -> dict[str, Any] | None:
        return self.values.get(key)

    async def set_json(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        if ttl_seconds > 0:
            self.values[key] = value


def _calibrated_gate(*, running_reranker_revision: str = RERANKER_REVISION) -> ConfidenceGate:
    configuration = GateConfiguration(
        schema_version=1,
        artifact_id="p6-test-gate-v1",
        calibrated=True,
        dataset=DatasetProvenance(
            name="p6-synthetic-gate-cases",
            version="1",
            sha256="a" * 64,
            examples=4,
        ),
        models=ModelProvenance(
            embedding_model="BAAI/bge-m3",
            embedding_revision=EMBEDDING_REVISION,
            reranker_model="BAAI/bge-reranker-v2-m3",
            reranker_revision=RERANKER_REVISION,
        ),
        thresholds=GateThresholds(
            top_score_min=0.7,
            score_margin_min=0.1,
            exact_score_min=0.85,
            min_evidence=2,
        ),
    )
    return ConfidenceGate(
        GateArtifact(configuration=configuration, sha256="b" * 64),
        embedding_model="BAAI/bge-m3",
        embedding_revision=EMBEDDING_REVISION,
        reranker_model="BAAI/bge-reranker-v2-m3",
        reranker_revision=running_reranker_revision,
    )


def _reranked(
    candidate: EvidenceCandidate,
    *,
    rank: int,
    score: float = 0.9,
) -> RerankedEvidence:
    return RerankedEvidence(candidate=candidate, rerank_score=score, rerank_rank=rank)


def _evidence(
    candidate_id: str,
    text: str,
    *,
    rank: int,
    source: str = "document-1",
    section: str = "section-1",
    char_start: int = 0,
    char_end: int | None = None,
    exact_terms: tuple[str, ...] = (),
    hash_salt: str = "",
) -> EvidenceCandidate:
    lexical = text.casefold()
    resolved_end = char_end if char_end is not None else max(1, len(text))
    return EvidenceCandidate(
        candidate_id=candidate_id,
        source_type="document",
        source_key=source,
        section_key=section,
        title=f"Document {source}",
        source_filename=f"{source}.txt",
        page_start=1,
        page_end=1,
        char_start=char_start,
        char_end=resolved_end,
        text_original=text,
        text_lexical=lexical,
        content_sha256=_sha(f"content:{text}:{hash_salt}"),
        lexical_sha256=_sha(f"lexical:{lexical}:{hash_salt}"),
        retrieval_rank=rank,
        retrieval_score=1 / (60 + rank),
        matched_exact_terms=exact_terms,
    )


def _web_evidence(candidate_id: str, *, rank: int, domain: str) -> EvidenceCandidate:
    text = f"web evidence {candidate_id}"
    return EvidenceCandidate(
        candidate_id=candidate_id,
        source_type="web",
        source_key=f"https://{domain}/{candidate_id}",
        section_key=f"https://{domain}/{candidate_id}#main",
        domain=domain,
        title=f"Web {candidate_id}",
        uri=f"https://{domain}/{candidate_id}",
        text_original=text,
        text_lexical=text,
        content_sha256=_sha(f"content:{text}"),
        lexical_sha256=_sha(f"lexical:{text}"),
        retrieval_rank=rank,
        retrieval_score=1 / (60 + rank),
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
