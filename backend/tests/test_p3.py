from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from argon2 import PasswordHasher, Type
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.dependencies import Principal
from app.auth.rate_limit import RateLimitDecision
from app.auth.security import SecurityService
from app.chat.service import IdempotencyConflictError, persist_user_message
from app.config import Settings
from app.core.idempotency import (
    ClaimState,
    canonical_request_hash,
    claim_idempotency,
    complete_idempotency,
    fail_idempotency,
)
from app.main import create_app
from app.models import (
    Base,
    ChatSession,
    Document,
    Feedback,
    Message,
    Tenant,
    User,
    UserTenant,
)


class FakeChecker:
    async def check(self) -> dict[str, bool]:
        return {"postgres": True}

    async def close(self) -> None:
        pass


class AllowRateLimiter:
    async def check(
        self, scope: str, identifiers: dict[str, str], limits: list[str]
    ) -> RateLimitDecision:
        return RateLimitDecision(True)

    async def register_failure(self, subject: str) -> None:
        pass

    async def clear_failures(self, subject: str) -> None:
        pass

    async def close(self) -> None:
        pass


@dataclass(frozen=True, slots=True)
class P3Ids:
    tenant: uuid.UUID
    other_tenant: uuid.UUID
    admin: uuid.UUID
    member: uuid.UUID
    peer: uuid.UUID
    other_user: uuid.UUID
    document: uuid.UUID
    other_document: uuid.UUID
    member_session: uuid.UUID
    peer_session: uuid.UUID


@dataclass(frozen=True, slots=True)
class P3Context:
    client: httpx.AsyncClient
    factory: async_sessionmaker[AsyncSession]
    ids: P3Ids
    tokens: dict[str, str]

    def authorization(self, identity: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[identity]}"}


@asynccontextmanager
async def p3_context(settings: Settings) -> AsyncIterator[P3Context]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    security = SecurityService(
        settings,
        password_hasher=PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, type=Type.ID),
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    ids, tokens = await _seed(factory, security)
    app = create_app(
        settings,
        FakeChecker(),
        session_factory=factory,
        rate_limiter=AllowRateLimiter(),
        security=security,
    )
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://rag.example.com"
            ) as client:
                yield P3Context(client=client, factory=factory, ids=ids, tokens=tokens)
    finally:
        await engine.dispose()


async def _seed(
    factory: async_sessionmaker[AsyncSession], security: SecurityService
) -> tuple[P3Ids, dict[str, str]]:
    tenant = Tenant(slug="shared", name="Shared")
    other_tenant = Tenant(slug="other", name="Other")
    password_hash = security.hash_password("Isolated test password 1!")
    admin = User(email="admin@example.com", password_hash=password_hash, is_superuser=True)
    member = User(email="member@example.com", password_hash=password_hash)
    peer = User(email="peer@example.com", password_hash=password_hash)
    other_user = User(email="other@example.com", password_hash=password_hash)
    async with factory() as session:
        session.add_all([tenant, other_tenant, admin, member, peer, other_user])
        await session.flush()
        session.add_all(
            [
                UserTenant(user_id=admin.id, tenant_id=tenant.id, role="admin"),
                UserTenant(user_id=member.id, tenant_id=tenant.id, role="member"),
                UserTenant(user_id=peer.id, tenant_id=tenant.id, role="member"),
                UserTenant(user_id=other_user.id, tenant_id=other_tenant.id, role="admin"),
            ]
        )
        await session.flush()
        document = Document(
            tenant_id=tenant.id,
            title="Tenant document",
            source_filename="tenant.pdf",
            mime="application/pdf",
            created_by=admin.id,
        )
        other_document = Document(
            tenant_id=other_tenant.id,
            title="Other document",
            source_filename="other.pdf",
            mime="application/pdf",
            created_by=other_user.id,
        )
        member_session = ChatSession(tenant_id=tenant.id, user_id=member.id, title="Member private")
        peer_session = ChatSession(tenant_id=tenant.id, user_id=peer.id, title="Peer private")
        session.add_all([document, other_document, member_session, peer_session])
        await session.commit()
    identities = {
        "admin": (admin, tenant, "admin"),
        "member": (member, tenant, "member"),
        "peer": (peer, tenant, "member"),
        "other": (other_user, other_tenant, "admin"),
    }
    tokens = {
        name: security.issue_access_token(
            user_id=user.id,
            tenant_id=identity_tenant.id,
            role=role,
            auth_version=user.auth_version,
        )[0]
        for name, (user, identity_tenant, role) in identities.items()
    }
    return (
        P3Ids(
            tenant=tenant.id,
            other_tenant=other_tenant.id,
            admin=admin.id,
            member=member.id,
            peer=peer.id,
            other_user=other_user.id,
            document=document.id,
            other_document=other_document.id,
            member_session=member_session.id,
            peer_session=peer_session.id,
        ),
        tokens,
    )


async def test_private_session_crud_cursor_and_ownership(settings: Settings) -> None:
    async with p3_context(settings) as context:
        headers = context.authorization("member")
        created_ids: list[str] = []
        for title in ("One", "Two", "Three"):
            response = await context.client.post(
                "/api/sessions", json={"title": title}, headers=headers
            )
            assert response.status_code == 201
            created_ids.append(response.json()["id"])

        first_page = await context.client.get("/api/sessions?limit=2", headers=headers)
        assert first_page.status_code == 200
        assert len(first_page.json()["items"]) == 2
        cursor = first_page.json()["next_cursor"]
        assert cursor is not None
        second_page = await context.client.get(
            "/api/sessions", params={"limit": 2, "cursor": cursor}, headers=headers
        )
        assert second_page.status_code == 200
        assert len(second_page.json()["items"]) >= 1
        tampered = await context.client.get(
            "/api/sessions", params={"cursor": f"{cursor}x"}, headers=headers
        )
        assert tampered.status_code == 422

        for identity in ("peer", "other"):
            denied = await context.client.get(
                f"/api/sessions/{created_ids[0]}/messages",
                headers=context.authorization(identity),
            )
            assert denied.status_code == 404
        deleted = await context.client.delete(f"/api/sessions/{created_ids[0]}", headers=headers)
        assert deleted.status_code == 204
        hidden = await context.client.get(
            f"/api/sessions/{created_ids[0]}/messages", headers=headers
        )
        assert hidden.status_code == 404


async def test_message_pagination_and_feedback_isolation(settings: Settings) -> None:
    async with p3_context(settings) as context:
        async with context.factory() as session:
            user_message = Message(
                tenant_id=context.ids.tenant,
                session_id=context.ids.member_session,
                user_id=context.ids.member,
                role="user",
                content="Question",
                meta={},
            )
            assistant_message = Message(
                tenant_id=context.ids.tenant,
                session_id=context.ids.member_session,
                user_id=context.ids.member,
                role="assistant",
                content="Answer [S1]",
                meta={"route": "documents"},
            )
            peer_message = Message(
                tenant_id=context.ids.tenant,
                session_id=context.ids.peer_session,
                user_id=context.ids.peer,
                role="assistant",
                content="Peer answer",
                meta={},
            )
            session.add_all([user_message, assistant_message, peer_message])
            await session.commit()

        headers = context.authorization("member")
        first = await context.client.get(
            f"/api/sessions/{context.ids.member_session}/messages?limit=1", headers=headers
        )
        assert first.status_code == 200
        assert len(first.json()["items"]) == 1
        second = await context.client.get(
            f"/api/sessions/{context.ids.member_session}/messages",
            params={"limit": 1, "cursor": first.json()["next_cursor"]},
            headers=headers,
        )
        assert second.status_code == 200
        assert len(second.json()["items"]) == 1

        created = await context.client.post(
            f"/api/messages/{assistant_message.id}/feedback",
            json={"rating": 1, "comment": "Useful"},
            headers=headers,
        )
        updated = await context.client.post(
            f"/api/messages/{assistant_message.id}/feedback",
            json={"rating": -1, "comment": "Needs detail"},
            headers=headers,
        )
        user_denied = await context.client.post(
            f"/api/messages/{user_message.id}/feedback", json={"rating": 1}, headers=headers
        )
        peer_denied = await context.client.post(
            f"/api/messages/{peer_message.id}/feedback", json={"rating": 1}, headers=headers
        )
        assert created.status_code == updated.status_code == 200
        assert updated.json()["rating"] == -1
        assert user_denied.status_code == peer_denied.status_code == 404
        async with context.factory() as session:
            count = await session.scalar(select(func.count()).select_from(Feedback))
            assert count == 1


async def test_collection_crud_membership_and_revision(settings: Settings) -> None:
    async with p3_context(settings) as context:
        member_denied = await context.client.post(
            "/api/collections",
            json={"name": "Policies"},
            headers=context.authorization("member"),
        )
        assert member_denied.status_code == 403
        headers = context.authorization("admin")
        created = await context.client.post(
            "/api/collections",
            json={"name": "Policies", "description": "Primary"},
            headers=headers,
        )
        assert created.status_code == 201
        collection_id = created.json()["collection"]["id"]
        assert created.json()["retrieval_revision"] == 2
        duplicate = await context.client.post(
            "/api/collections", json={"name": "POLICIES"}, headers=headers
        )
        assert duplicate.status_code == 409
        renamed = await context.client.patch(
            f"/api/collections/{collection_id}",
            json={"name": "Procedures"},
            headers=headers,
        )
        assert renamed.json()["retrieval_revision"] == 3
        added = await context.client.put(
            f"/api/collections/{collection_id}/documents/{context.ids.document}",
            headers=headers,
        )
        duplicate_add = await context.client.put(
            f"/api/collections/{collection_id}/documents/{context.ids.document}",
            headers=headers,
        )
        assert added.json() == {"retrieval_revision": 4, "changed": True}
        assert duplicate_add.json() == {"retrieval_revision": 4, "changed": False}
        cross_tenant = await context.client.put(
            f"/api/collections/{collection_id}/documents/{context.ids.other_document}",
            headers=headers,
        )
        assert cross_tenant.status_code == 404
        removed = await context.client.delete(
            f"/api/collections/{collection_id}/documents/{context.ids.document}",
            headers=headers,
        )
        no_op_remove = await context.client.delete(
            f"/api/collections/{collection_id}/documents/{context.ids.document}",
            headers=headers,
        )
        assert removed.json() == {"retrieval_revision": 5, "changed": True}
        assert no_op_remove.json() == {"retrieval_revision": 5, "changed": False}
        deleted = await context.client.delete(f"/api/collections/{collection_id}", headers=headers)
        assert deleted.json() == {"retrieval_revision": 6, "changed": True}
        listed = await context.client.get(
            "/api/collections", headers=context.authorization("member")
        )
        assert listed.json() == {"items": [], "retrieval_revision": 6}


async def test_idempotency_conflict_replay_and_stale_recovery(settings: Settings) -> None:
    async with p3_context(settings) as context:
        request_hash = canonical_request_hash({"b": 2, "a": 1})
        assert request_hash == canonical_request_hash({"a": 1, "b": 2})
        key = "request-0001"
        async with context.factory() as session:
            claimed = await claim_idempotency(
                session,
                tenant_id=context.ids.tenant,
                user_id=context.ids.member,
                operation="upload",
                key=key,
                request_hash=request_hash,
                ttl_seconds=60,
            )
            assert claimed.state is ClaimState.CLAIMED
            await complete_idempotency(
                session,
                tenant_id=context.ids.tenant,
                user_id=context.ids.member,
                operation="upload",
                key=key,
                response={"status": "accepted"},
                resource_id=context.ids.document,
            )
            await session.commit()
        async with context.factory() as session:
            replay = await claim_idempotency(
                session,
                tenant_id=context.ids.tenant,
                user_id=context.ids.member,
                operation="upload",
                key=key,
                request_hash=request_hash,
                ttl_seconds=60,
            )
            conflict = await claim_idempotency(
                session,
                tenant_id=context.ids.tenant,
                user_id=context.ids.member,
                operation="upload",
                key=key,
                request_hash=canonical_request_hash({"different": True}),
                ttl_seconds=60,
            )
            assert replay.state is ClaimState.REPLAY
            assert replay.response == {"status": "accepted"}
            assert replay.resource_id == context.ids.document
            assert conflict.state is ClaimState.CONFLICT

        past = datetime.now(UTC) - timedelta(minutes=5)
        active_key = "request-active"
        async with context.factory() as session:
            active = await claim_idempotency(
                session,
                tenant_id=context.ids.tenant,
                user_id=context.ids.member,
                operation="chat",
                key=active_key,
                request_hash=request_hash,
                ttl_seconds=60,
            )
            assert active.state is ClaimState.CLAIMED
            await session.commit()
        async with context.factory() as session:
            duplicate_active = await claim_idempotency(
                session,
                tenant_id=context.ids.tenant,
                user_id=context.ids.member,
                operation="chat",
                key=active_key,
                request_hash=request_hash,
                ttl_seconds=60,
            )
            assert duplicate_active.state is ClaimState.IN_PROGRESS

        stale_key = "request-stale"
        async with context.factory() as session:
            stale = await claim_idempotency(
                session,
                tenant_id=context.ids.tenant,
                user_id=context.ids.member,
                operation="chat",
                key=stale_key,
                request_hash=request_hash,
                ttl_seconds=10,
                now=past,
            )
            assert stale.state is ClaimState.CLAIMED
            await session.commit()
        async with context.factory() as session:
            recovered = await claim_idempotency(
                session,
                tenant_id=context.ids.tenant,
                user_id=context.ids.member,
                operation="chat",
                key=stale_key,
                request_hash=request_hash,
                ttl_seconds=60,
            )
            assert recovered.state is ClaimState.RECOVERED
            await fail_idempotency(
                session,
                tenant_id=context.ids.tenant,
                user_id=context.ids.member,
                operation="chat",
                key=stale_key,
            )
            await session.commit()


async def test_idempotent_message_persistence_creates_one_row(settings: Settings) -> None:
    async with p3_context(settings) as context:
        principal = Principal(
            user_id=context.ids.member,
            tenant_id=context.ids.tenant,
            email="member@example.com",
            role="member",
            is_superuser=False,
        )
        client_request_id = uuid.uuid4()
        async with context.factory() as session:
            first = await persist_user_message(
                session,
                principal=principal,
                session_id=context.ids.member_session,
                content="One question",
                client_request_id=client_request_id,
                idempotency_key="message-0001",
                ttl_seconds=60,
            )
        async with context.factory() as session:
            replay = await persist_user_message(
                session,
                principal=principal,
                session_id=context.ids.member_session,
                content="One question",
                client_request_id=client_request_id,
                idempotency_key="message-0001",
                ttl_seconds=60,
            )
            with pytest.raises(IdempotencyConflictError):
                await persist_user_message(
                    session,
                    principal=principal,
                    session_id=context.ids.member_session,
                    content="Changed question",
                    client_request_id=client_request_id,
                    idempotency_key="message-0001",
                    ttl_seconds=60,
                )
        assert not first.replayed
        assert replay.replayed
        assert first.message.id == replay.message.id
        async with context.factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.client_request_id == client_request_id)
            )
            assert count == 1
