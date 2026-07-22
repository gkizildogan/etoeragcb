from __future__ import annotations

import asyncio
import ipaddress
from typing import Protocol
from urllib.parse import urljoin

from pydantic import BaseModel, ConfigDict, Field

from app.web.extract import extract_text
from app.web.http import REDIRECT_STATUSES, HttpResponse
from app.web.security import (
    DnsResolver,
    ValidatedUrl,
    WebFetchError,
    validate_public_addresses,
    validate_url,
)


class HttpTransport(Protocol):
    async def fetch_once(
        self,
        url: ValidatedUrl,
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        *,
        max_bytes: int,
        timeout_seconds: float,
    ) -> HttpResponse: ...


class FetchedPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    final_url: str = Field(max_length=4096)
    domain: str = Field(max_length=253)
    text: str
    content_type: str
    bytes_received: int = Field(ge=0)
    redirect_count: int = Field(ge=0)
    resolved_ip: str


class SafePageFetcher:
    def __init__(
        self,
        resolver: DnsResolver,
        transport: HttpTransport,
        *,
        allowed_ports: frozenset[int],
        timeout_seconds: float,
        max_bytes: int,
        max_redirects: int,
        max_text_chars: int,
    ) -> None:
        self._resolver = resolver
        self._transport = transport
        self._allowed_ports = allowed_ports
        self._timeout_seconds = timeout_seconds
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._max_text_chars = max_text_chars

    async def fetch(self, url: str) -> FetchedPage:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await self._fetch_redirect_chain(url)
        except TimeoutError as exc:
            raise WebFetchError("timeout", "page fetch exceeded its total deadline") from exc

    async def _fetch_redirect_chain(self, url: str) -> FetchedPage:
        current = url
        visited: set[str] = set()
        for redirect_count in range(self._max_redirects + 1):
            validated = validate_url(current, self._allowed_ports)
            if validated.canonical_url in visited:
                raise WebFetchError("redirect_loop", "redirect loop detected")
            visited.add(validated.canonical_url)
            addresses = await self._resolver.resolve(validated.host, validated.port)
            validate_public_addresses(addresses)
            pinned_address = addresses[0]
            response = await self._transport.fetch_once(
                validated,
                pinned_address,
                max_bytes=self._max_bytes,
                timeout_seconds=self._timeout_seconds,
            )
            if response.status in REDIRECT_STATUSES:
                location = response.header("location")
                if location is None:
                    raise WebFetchError("invalid_redirect", "redirect Location is missing")
                if redirect_count >= self._max_redirects:
                    raise WebFetchError("redirect_limit", "redirect limit exceeded")
                current = urljoin(validated.canonical_url, location)
                continue
            if not 200 <= response.status < 300:
                raise WebFetchError("http_status", "page returned a non-success status")
            text, media_type = extract_text(
                response.body,
                response.header("content-type"),
                self._max_text_chars,
            )
            return FetchedPage(
                final_url=validated.canonical_url,
                domain=validated.host,
                text=text,
                content_type=media_type,
                bytes_received=len(response.body),
                redirect_count=redirect_count,
                resolved_ip=str(pinned_address),
            )
        raise WebFetchError("redirect_limit", "redirect limit exceeded")
