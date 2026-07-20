from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import SecurityService, normalize_email
from app.models import RefreshToken, Tenant, User, UserTenant


class AuthenticationDenied(Exception):
    pass


class RefreshReuseDetected(AuthenticationDenied):
    pass


@dataclass(frozen=True, slots=True)
class TokenBundle:
    access_token: str
    access_expires_in: int
    refresh_token: str
    refresh_expires_in: int
    tenant_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class MembershipView:
    tenant_id: uuid.UUID
    slug: str
    name: str
    role: str


class AuthService:
    def __init__(self, security: SecurityService) -> None:
        self.security = security

    async def login(
        self,
        session: AsyncSession,
        *,
        email: str,
        password: str,
        requested_tenant_id: uuid.UUID | None,
    ) -> TokenBundle:
        normalized_email = normalize_email(email)
        user = await session.scalar(select(User).where(User.email == normalized_email))
        password_valid = self.security.verify_password(
            user.password_hash if user is not None else None, password
        )
        if user is None or not password_valid or not user.is_active or user.disabled_at is not None:
            if user is not None:
                user.failed_login_count += 1
                user.last_failed_login_at = datetime.now(UTC)
                await session.commit()
            raise AuthenticationDenied

        memberships = await self._memberships(session, user.id)
        membership = self._select_membership(memberships, requested_tenant_id)
        if membership is None:
            user.failed_login_count += 1
            user.last_failed_login_at = datetime.now(UTC)
            await session.commit()
            raise AuthenticationDenied

        if self.security.password_needs_rehash(user.password_hash):
            user.password_hash = self.security.hash_password(password)
        user.failed_login_count = 0
        user.last_failed_login_at = None
        bundle = self._new_token_bundle(user, membership)
        session.add(bundle[1])
        await session.commit()
        return bundle[0]

    async def refresh(self, session: AsyncSession, *, raw_token: str) -> TokenBundle:
        now = datetime.now(UTC)
        token_hash = self.security.hash_refresh_token(raw_token)
        refresh = await session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        )
        if refresh is None:
            raise AuthenticationDenied

        if refresh.revoked_at is not None or refresh.replaced_by is not None:
            await self._revoke_family(session, refresh.family_id, now)
            await session.commit()
            raise RefreshReuseDetected

        if _as_utc(refresh.expires_at) <= now:
            refresh.revoked_at = now
            await session.commit()
            raise AuthenticationDenied

        row = (
            await session.execute(
                select(User, UserTenant)
                .join(
                    UserTenant,
                    (UserTenant.user_id == User.id) & (UserTenant.tenant_id == refresh.tenant_id),
                )
                .where(User.id == refresh.user_id)
            )
        ).one_or_none()
        if row is None:
            await self._revoke_family(session, refresh.family_id, now)
            await session.commit()
            raise AuthenticationDenied
        user, membership = row
        if not user.is_active or user.disabled_at is not None:
            await self._revoke_family(session, refresh.family_id, now)
            await session.commit()
            raise AuthenticationDenied

        raw_next = self.security.new_refresh_token()
        next_refresh = RefreshToken(
            id=uuid.uuid4(),
            user_id=user.id,
            tenant_id=membership.tenant_id,
            token_hash=self.security.hash_refresh_token(raw_next),
            family_id=refresh.family_id,
            expires_at=now + timedelta(seconds=self.security.refresh_ttl),
        )
        refresh.revoked_at = now
        refresh.replaced_by = next_refresh.id
        session.add(next_refresh)
        access_token, _ = self.security.issue_access_token(
            user_id=user.id,
            tenant_id=membership.tenant_id,
            role=membership.role,
            auth_version=user.auth_version,
            now=now,
        )
        await session.commit()
        return TokenBundle(
            access_token=access_token,
            access_expires_in=self.security.access_ttl,
            refresh_token=raw_next,
            refresh_expires_in=self.security.refresh_ttl,
            tenant_id=membership.tenant_id,
        )

    async def logout(self, session: AsyncSession, *, raw_token: str) -> None:
        token_hash = self.security.hash_refresh_token(raw_token)
        refresh = await session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        )
        if refresh is not None and refresh.revoked_at is None:
            refresh.revoked_at = datetime.now(UTC)
            await session.commit()

    async def memberships(self, session: AsyncSession, user_id: uuid.UUID) -> list[MembershipView]:
        return await self._memberships(session, user_id)

    async def _memberships(self, session: AsyncSession, user_id: uuid.UUID) -> list[MembershipView]:
        rows = (
            await session.execute(
                select(UserTenant, Tenant)
                .join(Tenant, Tenant.id == UserTenant.tenant_id)
                .where(UserTenant.user_id == user_id)
                .order_by(Tenant.created_at, Tenant.id)
            )
        ).all()
        return [
            MembershipView(
                tenant_id=membership.tenant_id,
                slug=tenant.slug,
                name=tenant.name,
                role=membership.role,
            )
            for membership, tenant in rows
        ]

    def _select_membership(
        self,
        memberships: list[MembershipView],
        requested_tenant_id: uuid.UUID | None,
    ) -> MembershipView | None:
        if requested_tenant_id is None:
            return memberships[0] if memberships else None
        return next((item for item in memberships if item.tenant_id == requested_tenant_id), None)

    def _new_token_bundle(
        self, user: User, membership: MembershipView
    ) -> tuple[TokenBundle, RefreshToken]:
        now = datetime.now(UTC)
        access_token, _ = self.security.issue_access_token(
            user_id=user.id,
            tenant_id=membership.tenant_id,
            role=membership.role,
            auth_version=user.auth_version,
            now=now,
        )
        raw_refresh = self.security.new_refresh_token()
        record = RefreshToken(
            user_id=user.id,
            tenant_id=membership.tenant_id,
            token_hash=self.security.hash_refresh_token(raw_refresh),
            family_id=uuid.uuid4(),
            expires_at=now + timedelta(seconds=self.security.refresh_ttl),
        )
        return (
            TokenBundle(
                access_token=access_token,
                access_expires_in=self.security.access_ttl,
                refresh_token=raw_refresh,
                refresh_expires_in=self.security.refresh_ttl,
                tenant_id=membership.tenant_id,
            ),
            record,
        )

    async def _revoke_family(
        self, session: AsyncSession, family_id: uuid.UUID, now: datetime
    ) -> None:
        await session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.family_id == family_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
