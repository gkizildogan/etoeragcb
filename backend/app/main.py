from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

import structlog
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.auth.rate_limit import RateLimiter, RedisRateLimiter
from app.auth.routes import router as auth_router
from app.auth.security import SecurityService
from app.collections.routes import router as collections_router
from app.config import Settings, get_settings
from app.core.db import create_database_engine, create_session_factory
from app.core.logging import configure_logging
from app.core.metrics import Metrics
from app.core.pagination import CursorCodec
from app.core.readiness import ReadinessChecker
from app.sessions.routes import router as sessions_router

logger = structlog.get_logger(__name__)


class Checker(Protocol):
    async def check(self) -> dict[str, bool]: ...

    async def close(self) -> None: ...


def create_app(
    settings: Settings | None = None,
    checker: Checker | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    rate_limiter: RateLimiter | None = None,
    security: SecurityService | None = None,
) -> FastAPI:
    configured = settings or get_settings()
    configure_logging(configured.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned_engine: AsyncEngine | None = None
        owned_rate_limiter: RateLimiter | None = None
        configured_factory = session_factory
        if configured_factory is None:
            owned_engine = create_database_engine(configured.resolved_database_url())
            configured_factory = create_session_factory(owned_engine)
        configured_limiter = rate_limiter
        if configured_limiter is None:
            redis = Redis.from_url(str(configured.redis_url), decode_responses=True)
            configured_limiter = RedisRateLimiter(redis)
            owned_rate_limiter = configured_limiter
        app.state.settings = configured
        app.state.metrics = Metrics()
        app.state.readiness = checker or ReadinessChecker(configured)
        app.state.session_factory = configured_factory
        app.state.rate_limiter = configured_limiter
        app.state.security = security or SecurityService(configured)
        app.state.cursor_codec = CursorCodec(
            configured.resolved_signing_secret().get_secret_value()
        )
        logger.info("application_started", environment=configured.app_env)
        try:
            yield
        finally:
            await app.state.readiness.close()
            if owned_rate_limiter is not None:
                await owned_rate_limiter.close()
            if owned_engine is not None:
                await owned_engine.dispose()
            logger.info("application_stopped")

    app = FastAPI(
        title="Public RAG Chatbot API",
        version="0.1.0",
        docs_url=None if configured.app_env == "production" else "/api/docs",
        redoc_url=None,
        openapi_url=None if configured.app_env == "production" else "/api/openapi.json",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=configured.allowed_hosts)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=configured.allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )
    app.include_router(auth_router)
    app.include_router(sessions_router)
    app.include_router(collections_router)

    @app.middleware("http")
    async def enforce_origin(request: Request, call_next: RequestResponseEndpoint) -> Response:
        origin = request.headers.get("origin")
        state_changing = request.method not in {"GET", "HEAD", "OPTIONS"}
        if (
            origin is not None
            and request.url.path.startswith("/api/")
            and state_changing
            and origin.rstrip("/") not in configured.allowed_origins
        ):
            return ORJSONResponse(
                {"detail": "Origin not allowed"}, status_code=status.HTTP_403_FORBIDDEN
            )
        return await call_next(request)

    @app.middleware("http")
    async def observe_request(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id")
        try:
            request_id = str(uuid.UUID(request_id)) if request_id else str(uuid.uuid4())
        except ValueError:
            request_id = str(uuid.uuid4())

        metrics: Metrics = request.app.state.metrics
        start = time.perf_counter()
        metrics.in_progress.inc()
        response: Response
        try:
            response = await call_next(request)
        except Exception:
            failed_route = getattr(request.scope.get("route"), "path", "unmatched")
            logger.exception(
                "request_failed",
                request_id=request_id,
                method=request.method,
                route=failed_route,
            )
            raise
        finally:
            metrics.in_progress.dec()

        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        duration = time.perf_counter() - start
        metrics.requests.labels(request.method, route_path, str(response.status_code)).inc()
        metrics.request_duration.labels(request.method, route_path).observe(duration)
        if request.url.path in {"/api/auth/login", "/api/auth/refresh"}:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_completed",
            request_id=request_id,
            method=request.method,
            route=route_path,
            status=response.status_code,
            duration_ms=round(duration * 1000, 2),
        )
        return response

    @app.get("/api/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/readyz", include_in_schema=False)
    async def readyz(request: Request) -> ORJSONResponse:
        results = await request.app.state.readiness.check()
        for dependency, ready in results.items():
            request.app.state.metrics.dependency_ready.labels(dependency).set(int(ready))
        ready = all(results.values())
        return ORJSONResponse(
            {"status": "ready" if ready else "not_ready"},
            status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.get("/api/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        return Response(
            generate_latest(request.app.state.metrics.registry),
            media_type=CONTENT_TYPE_LATEST,
        )

    return app
