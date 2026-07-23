from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import orjson
import pytest

from app.evaluation.calibration import classification_metrics, gate_answer, sweep_thresholds
from app.evaluation.dataset import DatasetError, load_dataset
from app.evaluation.engine import _available_records
from app.evaluation.feedback import FeedbackExportError, _record, write_feedback_jsonl
from app.evaluation.metrics import ranking_metrics
from app.evaluation.schemas import (
    CalibratedThresholds,
    CorpusRecord,
    GateObservation,
    GoldenQuery,
    QueryRanking,
)
from app.models import Feedback, Message
from app.rag.candidates import EvidenceCandidate
from app.rag.reranker import TeiReranker

GOLDEN_ROOT = Path("evaluation/golden/v1")


def test_golden_set_has_verified_provenance_and_required_coverage() -> None:
    dataset = load_dataset(GOLDEN_ROOT)

    assert dataset.manifest.license == "CC0-1.0"
    assert len(dataset.corpus) == 33
    assert len(dataset.queries) == 26
    assert {query.language for query in dataset.queries} == {"en", "tr"}
    assert sum(not query.answerable for query in dataset.queries) >= 5
    assert any(record.duplicate_group for record in dataset.corpus)
    assert {record.source_type for record in dataset.corpus} == {"document", "web"}


def test_dataset_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "golden"
    root.mkdir()
    for name in ("manifest.json", "corpus.jsonl", "queries.jsonl"):
        (root / name).write_bytes((GOLDEN_ROOT / name).read_bytes())
    with (root / "corpus.jsonl").open("ab") as output:
        output.write(b"\n")

    with pytest.raises(DatasetError, match="corpus hash"):
        load_dataset(root)


def test_scope_filters_documents_collections_headings_and_web() -> None:
    dataset = load_dataset(GOLDEN_ROOT)
    heading_query = next(query for query in dataset.queries if query.id == "q005")
    collection_query = next(query for query in dataset.queries if query.id == "q010")
    web_query = next(query for query in dataset.queries if query.id == "q014")

    heading = _available_records(dataset.corpus, heading_query, scoped=True)
    collection = _available_records(dataset.corpus, collection_query, scoped=True)
    web = _available_records(dataset.corpus, web_query, scoped=True)

    assert {record.id for record in heading} == {"c018"}
    assert all("engineering" in record.collection_ids for record in collection)
    assert any(record.source_type == "web" for record in web)
    assert all(
        record.source_type == "document"
        for record in _available_records(dataset.corpus, heading_query, scoped=False)
    )


def test_ranking_metrics_use_graded_relevance_and_distinct_sources() -> None:
    corpus = (
        _corpus("c001", document_id="doc-a"),
        _corpus("c002", document_id="doc-b"),
        _corpus("c003", document_id="doc-c"),
    )
    query = GoldenQuery(
        id="q001",
        language="en",
        category="semantic",
        query="relevant question",
        answerable=True,
        relevance={"c001": 3, "c002": 1},
    )
    ranking = QueryRanking(
        query_id="q001",
        ranked_ids=("c003", "c001", "c002"),
        scores=(0.9, 0.8, 0.7),
        latency_ms=12,
    )

    metrics = ranking_metrics((query,), (ranking,), corpus)

    assert metrics.recall_at_5 == 1.0
    assert metrics.mrr == 0.5
    assert 0.0 < metrics.ndcg_at_10 < 1.0
    assert metrics.mean_unique_sources_at_10 == 3.0
    assert metrics.latency_p95_ms == 12


def test_threshold_sweep_meets_precision_recall_and_matches_runtime_logic() -> None:
    observations = (
        _observation("q001", expected=True, top=0.95, margin=0.40, exact=0.95),
        _observation("q002", expected=True, top=0.88, margin=0.22),
        _observation("q003", expected=True, top=0.82, margin=0.18),
        _observation("q004", expected=False, top=0.70, margin=0.02),
        _observation("q005", expected=False, top=0.55, margin=0.20),
    )

    result = sweep_thresholds(
        observations,
        precision_target=1.0,
        recall_target=2 / 3,
    )

    assert result.targets_met is True
    assert result.metrics.precision == 1.0
    assert result.metrics.recall >= 2 / 3
    assert classification_metrics(observations, result.thresholds) == result.metrics
    assert gate_answer(observations[0], result.thresholds)
    assert not gate_answer(observations[-1], result.thresholds)


def test_singleton_margin_uses_zero_baseline_like_production_gate() -> None:
    singleton = _observation("q001", expected=True, top=0.95, margin=0.95)
    thresholds = CalibratedThresholds(
        top_score_min=0.9,
        score_margin_min=0.5,
        exact_score_min=1.0,
        min_evidence=1,
    )

    assert gate_answer(singleton, thresholds)


async def test_reranker_retries_rate_limit_without_parallel_batches() -> None:
    calls = 0
    active = 0
    maximum_active = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls, active, maximum_active
        calls += 1
        active += 1
        maximum_active = max(maximum_active, active)
        active -= 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(
            200,
            json=[{"index": 0, "score": 0.9}],
            request=request,
        )

    candidate = _evidence_candidate()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://tei",
    ) as client:
        reranker = TeiReranker(
            "http://tei",
            model_revision="a" * 40,
            max_candidates=1,
            batch_size=1,
            max_retries=1,
            client=client,
        )
        result = await reranker.rerank("query", (candidate,))

    assert result[0].rerank_score == 0.9
    assert calls == 2
    assert maximum_active == 1


def test_feedback_export_is_tenant_scoped_shape_and_mode_0600(tmp_path: Path) -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    user_message = Message(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        role="user",
        content="Private question",
        meta={},
        created_at=datetime.now(UTC),
    )
    assistant = Message(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        role="assistant",
        content="Private answer [S1]",
        meta={
            "user_message_id": str(user_message.id),
            "route": "rag",
            "citations": {"[S1]": {}},
            "retrieval": {
                "web_status": "disabled",
                "gate": {"route": "answer", "reasons": ["thresholds_passed"]},
            },
        },
        created_at=datetime.now(UTC),
    )
    feedback = Feedback(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        message_id=assistant.id,
        rating=1,
        comment="Useful",
        created_at=datetime.now(UTC),
    )
    record = _record(feedback, assistant, user_message, include_content=False)
    output = tmp_path / "feedback.jsonl"

    write_feedback_jsonl((record,), path=output, overwrite=False)
    exported = orjson.loads(output.read_bytes())

    assert exported["tenant_id"] == str(tenant_id)
    assert exported["rating"] == 1
    assert exported["citation_markers"] == ["[S1]"]
    assert "query" not in exported and "answer" not in exported
    assert len(exported["query_sha256"]) == 64
    assert os.stat(output).st_mode & 0o777 == 0o600
    with pytest.raises(FeedbackExportError):
        write_feedback_jsonl((record,), path=output, overwrite=False)


def _corpus(candidate_id: str, *, document_id: str) -> CorpusRecord:
    return CorpusRecord(
        id=candidate_id,
        source_type="document",
        document_id=document_id,
        document_title=document_id,
        source_filename=f"{document_id}.txt",
        heading="Heading",
        page=1,
        text=f"This is sufficiently long evaluation evidence for {candidate_id}.",
    )


def _observation(
    query_id: str,
    *,
    expected: bool,
    top: float,
    margin: float,
    exact: float | None = None,
) -> GateObservation:
    return GateObservation(
        query_id=query_id,
        expected_answer=expected,
        answerable=expected,
        relevant_in_context=expected,
        top_score=top,
        second_score=top - margin,
        score_margin=margin,
        best_exact_score=exact,
        evidence_count=3,
    )


def _evidence_candidate() -> EvidenceCandidate:
    text = "A sufficiently long candidate for reranking."
    return EvidenceCandidate(
        candidate_id="candidate",
        source_type="document",
        source_key="document",
        section_key="section",
        title="Candidate",
        source_filename="candidate.txt",
        page_start=1,
        page_end=1,
        char_start=0,
        char_end=len(text),
        text_original=text,
        text_lexical=text.casefold(),
        content_sha256="a" * 64,
        lexical_sha256="b" * 64,
        retrieval_rank=1,
        retrieval_score=1.0,
    )
