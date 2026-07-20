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
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.metrics import Metrics
from app.core.readiness import ReadinessChecker

logger = structlog.get_logger(__name__)


class Checker(Protocol):
    async def check(self) -> dict[str, bool]: ...

    async def close(self) -> None: ...


def create_app(settings: Settings | None = None, checker: Checker | None = None) -> FastAPI:
    configured = settings or get_settings()
    configure_logging(configured.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = configured
        app.state.metrics = Metrics()
        app.state.readiness = checker or ReadinessChecker(configured)
        logger.info("application_started", environment=configured.app_env)
        try:
            yield
        finally:
            await app.state.readiness.close()
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

    @app.middleware("http")
    async def observe_request(request: Request, call_next: object) -> Response:
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
            response = await call_next(request)  # type: ignore[operator]
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
