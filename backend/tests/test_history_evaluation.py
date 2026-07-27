from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.evaluation.history import (
    CandidateLabel,
    HistoryEvaluationError,
    QueryLabel,
    create_private_run_directory,
    historical_turns,
    write_private_jsonl,
)
from app.evaluation.history_scoring import (
    load_and_validate_labels,
    score_history_records,
)
from app.models import Base, ChatSession, Feedback, Message, Tenant, User, UserTenant


def test_private_run_permissions_and_overwrite_protection(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    create_private_run_directory(run_dir)
    output = run_dir / "examples.jsonl"
    write_private_jsonl(output, ({"schema_version": 1},))

    assert os.stat(run_dir).st_mode & 0o777 == 0o700
    assert os.stat(output).st_mode & 0o777 == 0o600
    with pytest.raises(HistoryEvaluationError, match="already exists"):
        write_private_jsonl(output, ())
    with pytest.raises(HistoryEvaluationError, match="already exists"):
        create_private_run_directory(run_dir)


async def test_historical_selection_is_tenant_scoped_and_excludes_deleted_sessions() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    tenant = Tenant(slug="history", name="History")
    other_tenant = Tenant(slug="other-history", name="Other")
    member = User(
        email="history-member@example.com",
        password_hash="test-fixture-hash",  # noqa: S106
    )
    superuser = User(
        email="history-super@example.com",
        password_hash="test-fixture-hash",  # noqa: S106
        is_superuser=True,
    )
    async with factory() as session:
        session.add_all([tenant, other_tenant, member, superuser])
        await session.flush()
        session.add_all(
            [
                UserTenant(user_id=member.id, tenant_id=tenant.id, role="member"),
                UserTenant(user_id=superuser.id, tenant_id=tenant.id, role="admin"),
                UserTenant(user_id=member.id, tenant_id=other_tenant.id, role="admin"),
            ]
        )
        active = ChatSession(tenant_id=tenant.id, user_id=superuser.id, title="Active")
        deleted = ChatSession(
            tenant_id=tenant.id,
            user_id=member.id,
            title="Deleted",
            deleted_at=datetime.now(UTC),
        )
        other = ChatSession(tenant_id=other_tenant.id, user_id=member.id, title="Other")
        session.add_all([active, deleted, other])
        await session.flush()
        for chat, owner in (
            (active, superuser),
            (deleted, member),
            (other, member),
        ):
            user_message = Message(
                tenant_id=chat.tenant_id,
                session_id=chat.id,
                user_id=owner.id,
                role="user",
                content="What is the policy?",
                meta={"document_ids": [], "collection_ids": [], "web_search": False},
            )
            session.add(user_message)
            await session.flush()
            assistant = Message(
                tenant_id=chat.tenant_id,
                session_id=chat.id,
                user_id=owner.id,
                role="assistant",
                content="The policy is documented.",
                meta={
                    "user_message_id": str(user_message.id),
                    "route": "answer",
                    "retrieval": {
                        "intent": "knowledge",
                        "gate": {"route": "answer"},
                        "sources": [],
                    },
                },
            )
            session.add(assistant)
            await session.flush()
            if chat is active:
                session.add(
                    Feedback(
                        tenant_id=tenant.id,
                        user_id=owner.id,
                        message_id=assistant.id,
                        rating=1,
                    )
                )
        await session.commit()
        turns = await historical_turns(session, tenant_id=tenant.id)

    await engine.dispose()
    assert len(turns) == 1
    assert turns[0].role_group == "superuser"
    assert turns[0].role_basis == "current_at_export"
    assert turns[0].feedback_rating == 1


def test_label_validation_rejects_missing_duplicate_unknown_and_suggestions(
    tmp_path: Path,
) -> None:
    example_id = str(uuid.uuid4())
    candidate_key = f"document:{uuid.uuid4()}:{uuid.uuid4()}"
    examples = [{"example_id": example_id}]
    candidates = [{"example_id": example_id, "candidate_key": candidate_key}]
    run_dir = tmp_path / "labels"
    create_private_run_directory(run_dir)
    query = _query_label(example_id)
    write_private_jsonl(run_dir / "labels.jsonl", (query,))
    with pytest.raises(HistoryEvaluationError, match="missing candidate"):
        load_and_validate_labels(run_dir, examples, candidates)

    write_private_jsonl(
        run_dir / "labels.jsonl",
        (query, query, _candidate_label(example_id, candidate_key)),
        overwrite=True,
    )
    with pytest.raises(HistoryEvaluationError, match="duplicate query"):
        load_and_validate_labels(run_dir, examples, candidates)

    suggestion = {
        **_candidate_label(example_id, candidate_key),
        "label_source": "llm_suggestion",
    }
    write_private_jsonl(run_dir / "labels.jsonl", (query, suggestion), overwrite=True)
    with pytest.raises(HistoryEvaluationError, match="human-confirmed"):
        load_and_validate_labels(run_dir, examples, candidates)


def test_scoring_detects_reranker_wins_ties_and_harms() -> None:
    example_ids = [str(uuid.uuid4()) for _ in range(3)]
    examples = [
        {
            "example_id": example_id,
            "role_group": "member",
            "scope_type": "unscoped",
            "feedback_rating": None,
            "corpus_comparability": "unchanged",
            "current_gate_route": "answer",
        }
        for example_id in example_ids
    ]
    candidates: list[dict[str, object]] = []
    query_labels: dict[str, QueryLabel] = {}
    candidate_labels: dict[tuple[str, str], CandidateLabel] = {}
    orders = (
        ((2, 1), (1, 0)),  # relevant candidate rises
        ((1, 1), (2, 0)),  # relevant candidate falls
        ((1, 1), (2, 0)),  # unchanged
    )
    post_orders = ((1, 2), (2, 1), (1, 2))
    for example_id, pre_order, post_order in zip(example_ids, orders, post_orders, strict=True):
        query_labels[example_id] = QueryLabel.model_validate(_query_label(example_id))
        for index, (pre_rank, relevance) in enumerate(pre_order):
            key = f"document:{uuid.uuid4()}:{uuid.uuid4()}"
            candidates.append(
                {
                    "example_id": example_id,
                    "candidate_key": key,
                    "current_retrieval_rank": pre_rank,
                    "current_rerank_rank": post_order[index],
                    "recorded_packed": False,
                    "recorded_rank": None,
                }
            )
            candidate_labels[(example_id, key)] = CandidateLabel.model_validate(
                _candidate_label(example_id, key, relevance=relevance)
            )

    report = score_history_records(examples, candidates, query_labels, candidate_labels)

    assert report["reranker_comparison"]["win_tie_loss"] == {
        "win": 1,
        "tie": 1,
        "loss": 1,
    }
    assert report["reranker_comparison"]["harmful_demotions"] == 1
    assert report["recorded_view"]["complete_pre_rerank_pool_available"] is False
    assert set(report["breakdowns"]) == {
        "role_group",
        "language",
        "scope_type",
        "feedback",
        "corpus_comparability",
    }


def _query_label(example_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "query",
        "example_id": example_id,
        "answerable": True,
        "language": "en",
        "label_source": "human",
        "reviewer": "reviewer-1",
        "reviewed_at": "2026-07-27T10:00:00Z",
        "notes": None,
    }


def _candidate_label(
    example_id: str, candidate_key: str, *, relevance: int = 3
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "candidate",
        "example_id": example_id,
        "candidate_key": candidate_key,
        "relevance": relevance,
        "label_source": "human",
        "reviewer": "reviewer-1",
        "reviewed_at": "2026-07-27T10:00:00Z",
        "notes": None,
    }
