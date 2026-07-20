from __future__ import annotations

import argparse
import asyncio
import getpass
import re
import uuid
from datetime import UTC, datetime

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import SecurityService, normalize_email
from app.config import get_settings
from app.core.db import create_database_engine, create_session_factory
from app.models import RefreshToken, Tenant, User, UserTenant

EMAIL_ADAPTER = TypeAdapter(EmailStr)
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Closed user and tenant administration")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap = subparsers.add_parser("bootstrap-admin")
    bootstrap.add_argument("--email", required=True)
    bootstrap.add_argument("--tenant-slug", default="shared")
    bootstrap.add_argument("--tenant-name", default="Shared Knowledge")

    create = subparsers.add_parser("create-user")
    _actor_argument(create)
    create.add_argument("--email", required=True)
    create.add_argument("--tenant-slug", default="shared")
    create.add_argument("--role", choices=["admin", "member"], default="member")
    create.add_argument("--superuser", action="store_true")

    for command in ("disable-user", "enable-user", "reset-password", "revoke-tokens"):
        action = subparsers.add_parser(command)
        _actor_argument(action)
        action.add_argument("--email", required=True)

    role = subparsers.add_parser("set-role")
    _actor_argument(role)
    role.add_argument("--email", required=True)
    role.add_argument("--tenant-slug", default="shared")
    role.add_argument("--role", choices=["admin", "member"], required=True)
    return parser


def _actor_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor-email", required=True)


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    security = SecurityService(settings)
    engine = create_database_engine(settings.resolved_database_url())
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            if args.command == "bootstrap-admin":
                await bootstrap_admin(session, security, args)
                return
            await authenticate_actor(session, security, args.actor_email)
            if args.command == "create-user":
                await create_user(session, security, args)
            elif args.command == "disable-user":
                await set_user_enabled(session, args.email, enabled=False)
            elif args.command == "enable-user":
                await set_user_enabled(session, args.email, enabled=True)
            elif args.command == "reset-password":
                await reset_password(session, security, args.email)
            elif args.command == "revoke-tokens":
                await revoke_tokens(session, args.email)
            elif args.command == "set-role":
                await set_role(session, args.email, args.tenant_slug, args.role)
            else:  # pragma: no cover - argparse prevents this
                raise RuntimeError("unsupported command")
    finally:
        await engine.dispose()


async def bootstrap_admin(
    session: AsyncSession, security: SecurityService, args: argparse.Namespace
) -> None:
    superusers = await session.scalar(
        select(func.count()).select_from(User).where(User.is_superuser.is_(True))
    )
    if superusers:
        raise RuntimeError("bootstrap refused: a superuser already exists")
    email = validated_email(args.email)
    if await session.scalar(select(User.id).where(User.email == email)) is not None:
        raise RuntimeError("bootstrap refused: that user already exists")
    slug = validated_slug(args.tenant_slug)
    tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
    if tenant is None:
        tenant = Tenant(slug=slug, name=args.tenant_name.strip())
        session.add(tenant)
        await session.flush()
    password_hash = security.hash_password(prompt_new_password())
    user = User(email=email, password_hash=password_hash, is_superuser=True)
    session.add(user)
    await session.flush()
    session.add(UserTenant(user_id=user.id, tenant_id=tenant.id, role="admin"))
    await session.commit()
    print(f"Created bootstrap administrator {email} in tenant {slug}.")


async def authenticate_actor(
    session: AsyncSession, security: SecurityService, actor_email: str
) -> User:
    email = validated_email(actor_email)
    actor = await session.scalar(select(User).where(User.email == email))
    password = getpass.getpass("Acting administrator password: ")
    if (
        actor is None
        or not actor.is_superuser
        or not actor.is_active
        or actor.disabled_at is not None
        or not security.verify_password(actor.password_hash, password)
    ):
        raise RuntimeError("administrator authentication failed")
    return actor


async def create_user(
    session: AsyncSession, security: SecurityService, args: argparse.Namespace
) -> None:
    email = validated_email(args.email)
    if await session.scalar(select(User.id).where(User.email == email)) is not None:
        raise RuntimeError("user already exists")
    slug = validated_slug(args.tenant_slug)
    tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
    if tenant is None:
        raise RuntimeError("tenant does not exist")
    user = User(
        email=email,
        password_hash=security.hash_password(prompt_new_password()),
        is_superuser=bool(args.superuser),
    )
    session.add(user)
    await session.flush()
    session.add(UserTenant(user_id=user.id, tenant_id=tenant.id, role=args.role))
    await session.commit()
    print(f"Created user {email} with role {args.role} in tenant {slug}.")


async def set_user_enabled(session: AsyncSession, raw_email: str, *, enabled: bool) -> None:
    user = await get_user(session, raw_email)
    now = datetime.now(UTC)
    user.is_active = enabled
    user.disabled_at = None if enabled else now
    user.auth_version += 1
    if not enabled:
        await _revoke_user_tokens(session, user.id, now)
    await session.commit()
    print(f"{'Enabled' if enabled else 'Disabled'} user {user.email}.")


async def reset_password(session: AsyncSession, security: SecurityService, raw_email: str) -> None:
    user = await get_user(session, raw_email)
    user.password_hash = security.hash_password(prompt_new_password())
    user.auth_version += 1
    await _revoke_user_tokens(session, user.id, datetime.now(UTC))
    await session.commit()
    print(f"Reset password and revoked sessions for {user.email}.")


async def revoke_tokens(session: AsyncSession, raw_email: str) -> None:
    user = await get_user(session, raw_email)
    user.auth_version += 1
    await _revoke_user_tokens(session, user.id, datetime.now(UTC))
    await session.commit()
    print(f"Revoked all sessions for {user.email}.")


async def set_role(session: AsyncSession, raw_email: str, raw_slug: str, role: str) -> None:
    user = await get_user(session, raw_email)
    slug = validated_slug(raw_slug)
    tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
    if tenant is None:
        raise RuntimeError("tenant does not exist")
    membership = await session.scalar(
        select(UserTenant).where(UserTenant.user_id == user.id, UserTenant.tenant_id == tenant.id)
    )
    if membership is None:
        membership = UserTenant(user_id=user.id, tenant_id=tenant.id, role=role)
        session.add(membership)
    else:
        membership.role = role
    user.auth_version += 1
    await _revoke_user_tokens(session, user.id, datetime.now(UTC))
    await session.commit()
    print(f"Set {user.email} role to {role} in tenant {slug} and revoked sessions.")


async def get_user(session: AsyncSession, raw_email: str) -> User:
    email = validated_email(raw_email)
    user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        raise RuntimeError("user does not exist")
    return user


async def _revoke_user_tokens(
    session: AsyncSession, user_id: uuid.UUID, revoked_at: datetime
) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=revoked_at)
    )


def prompt_new_password() -> str:
    first = getpass.getpass("New password (minimum 12 characters): ")
    second = getpass.getpass("Confirm new password: ")
    if first != second:
        raise RuntimeError("passwords do not match")
    return first


def validated_email(raw_email: str) -> str:
    try:
        return normalize_email(str(EMAIL_ADAPTER.validate_python(raw_email)))
    except ValidationError as exc:
        raise RuntimeError("invalid email address") from exc


def validated_slug(raw_slug: str) -> str:
    slug = raw_slug.strip().lower()
    if not SLUG_RE.fullmatch(slug) or len(slug) > 80:
        raise RuntimeError("tenant slug must contain lowercase letters, numbers, and hyphens")
    return slug


def main() -> None:
    try:
        asyncio.run(run(build_parser().parse_args()))
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
