from __future__ import annotations

from functools import lru_cache

import uvicorn
from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.web.fetcher import FetchedPage, SafePageFetcher
from app.web.http import PinnedHttpTransport
from app.web.security import SystemDnsResolver, WebFetchError


class FetchSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", frozen=True)

    web_fetch_timeout: float = Field(gt=0, le=60)
    web_max_bytes: int = Field(ge=1024, le=20_000_000)
    web_allowed_ports: list[int]
    web_max_redirects: int = Field(ge=0, le=10)
    web_text_max_chars: int = Field(ge=256, le=50_000)

    @field_validator("web_allowed_ports")
    @classmethod
    def validate_ports(cls, value: list[int]) -> list[int]:
        if not value or any(port not in {80, 443} for port in value):
            raise ValueError("WEB_ALLOWED_PORTS must contain only 80 and/or 443")
        return sorted(set(value))


class FetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(min_length=1, max_length=4096)


@lru_cache
def get_fetch_settings() -> FetchSettings:
    return FetchSettings()


def create_fetch_app(settings: FetchSettings | None = None) -> FastAPI:
    configured = settings or get_fetch_settings()
    fetcher = SafePageFetcher(
        SystemDnsResolver(),
        PinnedHttpTransport(),
        allowed_ports=frozenset(configured.web_allowed_ports),
        timeout_seconds=configured.web_fetch_timeout,
        max_bytes=configured.web_max_bytes,
        max_redirects=configured.web_max_redirects,
        max_text_chars=configured.web_text_max_chars,
    )
    app = FastAPI(
        title="Internal safe page fetcher",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/fetch", response_model=FetchedPage)
    async def fetch(request: FetchRequest) -> FetchedPage | JSONResponse:
        try:
            return await fetcher.fetch(request.url)
        except WebFetchError as exc:
            return JSONResponse(
                {"detail": "page rejected", "code": exc.code},
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )

    return app


def main() -> None:
    uvicorn.run(
        "app.web.serve:create_fetch_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - private Docker network only
        port=8081,
        proxy_headers=False,
        access_log=False,
        log_config="logging.json",
    )


if __name__ == "__main__":
    main()
