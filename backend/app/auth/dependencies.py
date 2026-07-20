from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import InvalidAccessTokenError
from app.core.db import get_db_session
from app.models import ChatSession, User, UserTenant

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    role: str
    is_superuser: bool


def authentication_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unable to authenticate",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> Principal:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise authentication_error()
    try:
        claims = request.app.state.security.decode_access_token(credentials.credentials)
    except InvalidAccessTokenError as exc:
        raise authentication_error() from exc

    row = (
        await session.execute(
            select(User, UserTenant)
            .join(
                UserTenant,
                (UserTenant.user_id == User.id) & (UserTenant.tenant_id == claims.tenant_id),
            )
            .where(User.id == claims.user_id)
        )
    ).one_or_none()
    if row is None:
        raise authentication_error()
    user, membership = row
    if (
        not user.is_active
        or user.disabled_at is not None
        or user.auth_version != claims.auth_version
    ):
        raise authentication_error()
    return Principal(
        user_id=user.id,
        tenant_id=membership.tenant_id,
        email=user.email,
        role=membership.role,
        is_superuser=user.is_superuser,
    )


async def require_tenant_admin(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    if principal.role != "admin" and not principal.is_superuser:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted")
    return principal


async def load_owned_session(
    session_id: uuid.UUID,
    principal: Principal,
    session: AsyncSession,
) -> ChatSession:
    owned = await session.scalar(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.tenant_id == principal.tenant_id,
            ChatSession.user_id == principal.user_id,
            ChatSession.deleted_at.is_(None),
        )
    )
    if owned is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return owned
