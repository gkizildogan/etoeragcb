from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
import pytest
from argon2 import PasswordHasher, Type
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.dependencies import Principal, load_owned_session
from app.auth.rate_limit import RateLimitDecision
from app.auth.security import SecurityService
from app.config import Settings
from app.main import create_app
from app.models import Base, ChatSession, Tenant, User, UserTenant

PASSWORD = "Correct horse battery 1!"  # noqa: S105 - isolated test credential
WRONG_PASSWORD = "wrong"  # noqa: S105 - isolated negative-path credential


class FakeChecker:
    async def check(self) -> dict[str, bool]:
        return {"postgres": True}

    async def close(self) -> None:
        pass


@dataclass
class MemoryRateLimiter:
    counts: dict[tuple[str, str, str, int], int] = field(default_factory=dict)

    async def check(
        self, scope: str, identifiers: dict[str, str], limits: list[str]
    ) -> RateLimitDecision:
        retry_after = 0
        for label, identifier in identifiers.items():
            for raw_limit in limits:
                maximum, window = (int(part) for part in raw_limit.split("/", maxsplit=1))
                key = (scope, label, identifier, window)
                self.counts[key] = self.counts.get(key, 0) + 1
                if self.counts[key] > maximum:
                    retry_after = max(retry_after, window)
        return RateLimitDecision(retry_after == 0, retry_after)

    async def register_failure(self, subject: str) -> None:
        pass

    async def clear_failures(self, subject: str) -> None:
        pass

    async def close(self) -> None:
        pass


@dataclass(frozen=True)
class SeededIds:
    tenant_one: uuid.UUID
    tenant_two: uuid.UUID
    user_one: uuid.UUID
    user_two: uuid.UUID
    disabled_user: uuid.UUID
    own_session: uuid.UUID
    peer_session: uuid.UUID
    other_tenant_session: uuid.UUID


@dataclass(frozen=True)
class AuthTestContext:
    client: httpx.AsyncClient
    factory: async_sessionmaker[AsyncSession]
    ids: SeededIds


@asynccontextmanager
async def auth_context(
    settings: Settings,
) -> AsyncIterator[AuthTestContext]:
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
    ids = await seed_database(factory, security)
    app = create_app(
        settings,
        FakeChecker(),
        session_factory=factory,
        rate_limiter=MemoryRateLimiter(),
        security=security,
    )
    transport = httpx.ASGITransport(app=app)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport, base_url="https://rag.example.com"
            ) as client:
                yield AuthTestContext(client=client, factory=factory, ids=ids)
    finally:
        await engine.dispose()


async def seed_database(
    factory: async_sessionmaker[AsyncSession], security: SecurityService
) -> SeededIds:
    tenant_one = Tenant(slug="shared", name="Shared Knowledge")
    tenant_two = Tenant(slug="second", name="Second Tenant")
    user_one = User(email="member@example.com", password_hash=security.hash_password(PASSWORD))
    user_two = User(email="peer@example.com", password_hash=security.hash_password(PASSWORD))
    disabled_user = User(
        email="disabled@example.com",
        password_hash=security.hash_password(PASSWORD),
        is_active=False,
    )
    async with factory() as session:
        session.add_all([tenant_one, tenant_two, user_one, user_two, disabled_user])
        await session.flush()
        session.add_all(
            [
                UserTenant(user_id=user_one.id, tenant_id=tenant_one.id, role="member"),
                UserTenant(user_id=user_one.id, tenant_id=tenant_two.id, role="admin"),
                UserTenant(user_id=user_two.id, tenant_id=tenant_one.id, role="member"),
                UserTenant(user_id=disabled_user.id, tenant_id=tenant_one.id, role="member"),
            ]
        )
        await session.flush()
        own = ChatSession(tenant_id=tenant_one.id, user_id=user_one.id, title="Private user one")
        peer = ChatSession(tenant_id=tenant_one.id, user_id=user_two.id, title="Private user two")
        other_tenant = ChatSession(
            tenant_id=tenant_two.id, user_id=user_one.id, title="Other tenant"
        )
        session.add_all([own, peer, other_tenant])
        await session.commit()
    return SeededIds(
        tenant_one=tenant_one.id,
        tenant_two=tenant_two.id,
        user_one=user_one.id,
        user_two=user_two.id,
        disabled_user=disabled_user.id,
        own_session=own.id,
        peer_session=peer.id,
        other_tenant_session=other_tenant.id,
    )


async def login(client: httpx.AsyncClient, **overrides: object) -> httpx.Response:
    payload: dict[str, object] = {"email": "member@example.com", "password": PASSWORD}
    payload.update(overrides)
    return await client.post("/api/auth/login", json=payload)


async def test_login_me_and_closed_registration(settings: Settings) -> None:
    async with auth_context(settings) as context:
        response = await login(context.client)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Pragma"] == "no-cache"
        payload = response.json()
        me = await context.client.get(
            "/api/me", headers={"Authorization": f"Bearer {payload['access_token']}"}
        )
        registration = await context.client.post(
            "/api/auth/register", json={"email": "new@example.com", "password": PASSWORD}
        )
    assert me.status_code == 200
    assert me.json()["email"] == "member@example.com"
    assert len(me.json()["memberships"]) == 2
    assert registration.status_code == 404


async def test_login_failures_are_generic(settings: Settings) -> None:
    async with auth_context(settings) as context:
        unknown = await login(context.client, email="unknown@example.com")
        wrong = await login(context.client, password=WRONG_PASSWORD)
        disabled = await login(context.client, email="disabled@example.com")
    assert {unknown.status_code, wrong.status_code, disabled.status_code} == {401}
    assert unknown.json() == wrong.json() == disabled.json() == {"detail": "Unable to sign in"}


async def test_login_is_throttled_by_client_ip(
    settings_values: dict[str, object],
) -> None:
    settings_values["login_rate_limits"] = ["2/60"]
    settings = Settings(**settings_values)
    async with auth_context(settings) as context:
        first = await login(context.client, password=WRONG_PASSWORD)
        second = await login(context.client, password=WRONG_PASSWORD)
        third = await login(context.client, password=WRONG_PASSWORD)
    assert first.status_code == second.status_code == 401
    assert third.status_code == 429
    assert third.json() == {"detail": "Unable to sign in"}
    assert third.headers["Retry-After"] == "60"


async def test_refresh_rotation_and_reuse_revokes_family(settings: Settings) -> None:
    async with auth_context(settings) as context:
        signed_in = await login(context.client)
        first_refresh = signed_in.json()["refresh_token"]
        rotated = await context.client.post(
            "/api/auth/refresh", json={"refresh_token": first_refresh}
        )
        second_refresh = rotated.json()["refresh_token"]
        reuse = await context.client.post(
            "/api/auth/refresh", json={"refresh_token": first_refresh}
        )
        family_revoked = await context.client.post(
            "/api/auth/refresh", json={"refresh_token": second_refresh}
        )
    assert rotated.status_code == 200
    assert reuse.status_code == family_revoked.status_code == 401


async def test_disabled_user_is_denied_on_every_authenticated_request(settings: Settings) -> None:
    async with auth_context(settings) as context:
        signed_in = await login(context.client)
        payload = signed_in.json()
        async with context.factory() as session:
            user = await session.get(User, context.ids.user_one)
            assert user is not None
            user.is_active = False
            user.auth_version += 1
            await session.commit()
        me = await context.client.get(
            "/api/me", headers={"Authorization": f"Bearer {payload['access_token']}"}
        )
        refresh = await context.client.post(
            "/api/auth/refresh", json={"refresh_token": payload["refresh_token"]}
        )
    assert me.status_code == refresh.status_code == 401


async def test_state_changing_request_rejects_disallowed_origin(settings: Settings) -> None:
    async with auth_context(settings) as context:
        rejected = await context.client.post(
            "/api/auth/login",
            json={"email": "member@example.com", "password": PASSWORD},
            headers={"Origin": "https://attacker.example"},
        )
        accepted = await context.client.post(
            "/api/auth/login",
            json={"email": "member@example.com", "password": PASSWORD},
            headers={"Origin": "https://rag.example.com"},
        )
    assert rejected.status_code == 403
    assert accepted.status_code == 200


async def test_tenant_selection_and_private_session_ownership(settings: Settings) -> None:
    async with auth_context(settings) as context:
        selected = await login(context.client, tenant_id=str(context.ids.tenant_two))
        assert selected.status_code == 200
        assert selected.json()["tenant_id"] == str(context.ids.tenant_two)

        principal = Principal(
            user_id=context.ids.user_one,
            tenant_id=context.ids.tenant_one,
            email="member@example.com",
            role="member",
            is_superuser=False,
        )
        async with context.factory() as session:
            owned = await load_owned_session(
                context.ids.own_session, principal=principal, session=session
            )
            assert owned.id == context.ids.own_session
            for forbidden in (
                context.ids.peer_session,
                context.ids.other_tenant_session,
                uuid.uuid4(),
            ):
                with pytest.raises(HTTPException) as exc_info:
                    await load_owned_session(forbidden, principal=principal, session=session)
                assert exc_info.value.status_code == 404


async def test_logout_revokes_refresh_token(settings: Settings) -> None:
    async with auth_context(settings) as context:
        signed_in = await login(context.client)
        refresh_token = signed_in.json()["refresh_token"]
        logout = await context.client.post(
            "/api/auth/logout", json={"refresh_token": refresh_token}
        )
        refresh = await context.client.post(
            "/api/auth/refresh", json={"refresh_token": refresh_token}
        )
    assert logout.status_code == 204
    assert refresh.status_code == 401
