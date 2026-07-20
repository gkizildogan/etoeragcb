from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal, get_current_principal
from app.auth.rate_limit import RateLimiter
from app.auth.schemas import (
    LoginRequest,
    LogoutRequest,
    MeResponse,
    RefreshRequest,
    TenantMembershipResponse,
    TokenResponse,
)
from app.auth.security import SecurityService, audit_hash, normalize_email
from app.auth.service import AuthenticationDenied, AuthService, RefreshReuseDetected, TokenBundle
from app.core.db import get_db_session

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/api")


def _login_failure() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unable to sign in",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _refresh_failure() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unable to refresh session",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _response(bundle: TokenBundle) -> TokenResponse:
    return TokenResponse(
        access_token=bundle.access_token,
        expires_in=bundle.access_expires_in,
        refresh_token=bundle.refresh_token,
        refresh_expires_in=bundle.refresh_expires_in,
        tenant_id=bundle.tenant_id,
    )


@router.post("/auth/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TokenResponse:
    settings = request.app.state.settings
    limiter: RateLimiter = request.app.state.rate_limiter
    account_key = audit_hash(normalize_email(str(body.email)))
    ip_key = audit_hash(_client_ip(request))
    decision = await limiter.check(
        "login", {"account": account_key, "ip": ip_key}, settings.login_rate_limits
    )
    if not decision.allowed:
        request.app.state.metrics.auth_events.labels("login_throttled").inc()
        logger.warning("login_throttled", account_hash=account_key, client_hash=ip_key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Unable to sign in",
            headers={"Retry-After": str(decision.retry_after)},
        )

    service = AuthService(request.app.state.security)
    try:
        bundle = await service.login(
            session,
            email=str(body.email),
            password=body.password,
            requested_tenant_id=body.tenant_id,
        )
    except AuthenticationDenied as exc:
        await limiter.register_failure(account_key)
        request.app.state.metrics.auth_events.labels("login_failure").inc()
        logger.info("login_failed", account_hash=account_key, client_hash=ip_key)
        raise _login_failure() from exc
    await limiter.clear_failures(account_key)
    request.app.state.metrics.auth_events.labels("login_success").inc()
    logger.info("login_succeeded", account_hash=account_key, tenant_id=str(bundle.tenant_id))
    return _response(bundle)


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TokenResponse:
    settings = request.app.state.settings
    limiter: RateLimiter = request.app.state.rate_limiter
    security: SecurityService = request.app.state.security
    ip_key = audit_hash(_client_ip(request))
    token_key = security.hash_refresh_token(body.refresh_token)[:16]
    decision = await limiter.check(
        "refresh", {"token": token_key, "ip": ip_key}, settings.login_rate_limits
    )
    if not decision.allowed:
        request.app.state.metrics.auth_events.labels("refresh_throttled").inc()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Unable to refresh session",
            headers={"Retry-After": str(decision.retry_after)},
        )
    try:
        bundle = await AuthService(security).refresh(session, raw_token=body.refresh_token)
    except RefreshReuseDetected as exc:
        request.app.state.metrics.auth_events.labels("refresh_reuse").inc()
        logger.warning("refresh_token_reuse", token_hash=token_key, client_hash=ip_key)
        raise _refresh_failure() from exc
    except AuthenticationDenied as exc:
        request.app.state.metrics.auth_events.labels("refresh_failure").inc()
        raise _refresh_failure() from exc
    request.app.state.metrics.auth_events.labels("refresh_success").inc()
    return _response(bundle)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: LogoutRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
    await AuthService(request.app.state.security).logout(session, raw_token=body.refresh_token)
    request.app.state.metrics.auth_events.labels("logout").inc()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=MeResponse)
async def me(
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> MeResponse:
    memberships = await AuthService(request.app.state.security).memberships(
        session, principal.user_id
    )
    return MeResponse(
        user_id=principal.user_id,
        email=principal.email,
        is_superuser=principal.is_superuser,
        active_tenant_id=principal.tenant_id,
        memberships=[
            TenantMembershipResponse(
                tenant_id=item.tenant_id,
                slug=item.slug,
                name=item.name,
                role=item.role,
                active=item.tenant_id == principal.tenant_id,
            )
            for item in memberships
        ],
    )
