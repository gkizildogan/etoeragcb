from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from app.config import Settings
from app.main import create_app


class FakeChecker:
    def __init__(self, values: dict[str, bool]) -> None:
        self.values = values
        self.closed = False

    async def check(self) -> dict[str, bool]:
        return self.values

    async def close(self) -> None:
        self.closed = True


@asynccontextmanager
async def api_client(settings: Settings, checker: FakeChecker) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings, checker)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="https://rag.example.com"
        ) as client:
            yield client


async def test_health_request_id_and_metrics(settings: Settings) -> None:
    checker = FakeChecker({"postgres": True, "redis": True})
    async with api_client(settings, checker) as client:
        response = await client.get("/api/healthz", headers={"X-Request-ID": "not-a-uuid"})
        metrics = await client.get("/api/metrics")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"] != "not-a-uuid"
    assert "rag_http_requests_total" in metrics.text
    assert checker.closed


async def test_readiness_is_generic_and_reports_failure(settings: Settings) -> None:
    checker = FakeChecker({"postgres": True, "redis": False})
    async with api_client(settings, checker) as client:
        response = await client.get("/api/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "redis" not in response.text


async def test_rejects_unknown_hosts_and_origins(settings: Settings) -> None:
    checker = FakeChecker({"postgres": True})
    async with api_client(settings, checker) as client:
        bad_host = await client.get("/api/healthz", headers={"Host": "attacker.example"})
        bad_origin = await client.options(
            "/api/healthz",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert bad_host.status_code == 400
    assert bad_origin.status_code == 400
