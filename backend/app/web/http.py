from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass

from app.web.security import ValidatedUrl, WebFetchError

HEADER_LIMIT = 65_536
CHUNK_LINE_LIMIT = 128
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: dict[str, tuple[str, ...]]
    body: bytes

    def header(self, name: str) -> str | None:
        values = self.headers.get(name.lower())
        return values[0] if values else None


class PinnedHttpTransport:
    """Minimal HTTP/1.1 client that connects to a prevalidated literal IP."""

    def __init__(self, *, user_agent: str = "etoerag-web-fetcher/1.0") -> None:
        self._user_agent = user_agent
        self._ssl_context = ssl.create_default_context()

    async def fetch_once(
        self,
        url: ValidatedUrl,
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        *,
        max_bytes: int,
        timeout_seconds: float,
    ) -> HttpResponse:
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(timeout_seconds):
                reader, writer = await asyncio.open_connection(
                    str(address),
                    url.port,
                    family=socket.AF_INET if address.version == 4 else socket.AF_INET6,
                    ssl=self._ssl_context if url.scheme == "https" else None,
                    server_hostname=url.host if url.scheme == "https" else None,
                    ssl_handshake_timeout=timeout_seconds if url.scheme == "https" else None,
                    limit=HEADER_LIMIT,
                )
                request = (
                    f"GET {url.request_target} HTTP/1.1\r\n"
                    f"Host: {url.host_header}\r\n"
                    f"User-Agent: {self._user_agent}\r\n"
                    "Accept: text/html, application/xhtml+xml, text/plain\r\n"
                    "Accept-Encoding: identity\r\n"
                    "Connection: close\r\n\r\n"
                )
                writer.write(request.encode("ascii"))
                await writer.drain()
                status, headers = await _read_response_head(reader)
                if status in REDIRECT_STATUSES:
                    return HttpResponse(status=status, headers=headers, body=b"")
                body = await _read_body(reader, headers, max_bytes)
                return HttpResponse(status=status, headers=headers, body=body)
        except TimeoutError as exc:
            raise WebFetchError("timeout", "page fetch exceeded its deadline") from exc
        except ssl.SSLError as exc:
            raise WebFetchError("tls_failure", "TLS validation failed") from exc
        except WebFetchError:
            raise
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            raise WebFetchError("connection_failure", "page connection failed") from exc
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()


async def _read_response_head(
    reader: asyncio.StreamReader,
) -> tuple[int, dict[str, tuple[str, ...]]]:
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
        raise WebFetchError("invalid_response", "HTTP response headers are invalid") from exc
    if len(raw) > HEADER_LIMIT:
        raise WebFetchError("invalid_response", "HTTP response headers are oversized")
    lines = raw[:-4].split(b"\r\n")
    if not lines:
        raise WebFetchError("invalid_response", "HTTP response status is missing")
    try:
        status_line = lines[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise WebFetchError("invalid_response", "HTTP status line is not ASCII") from exc
    match = re.fullmatch(r"HTTP/1\.[01] ([1-5][0-9]{2})(?: .*)?", status_line)
    if match is None:
        raise WebFetchError("invalid_response", "HTTP response status is malformed")
    mutable_headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        if not line or line[:1] in {b" ", b"\t"} or b":" not in line:
            raise WebFetchError("invalid_response", "HTTP response header is malformed")
        raw_name, raw_value = line.split(b":", 1)
        try:
            name = raw_name.decode("ascii").lower()
            value = raw_value.decode("latin-1").strip()
        except UnicodeDecodeError as exc:
            raise WebFetchError("invalid_response", "HTTP response header is invalid") from exc
        if re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", name) is None:
            raise WebFetchError("invalid_response", "HTTP response header name is invalid")
        if any(ord(character) < 32 and character != "\t" for character in value):
            raise WebFetchError("invalid_response", "HTTP response header value is invalid")
        mutable_headers.setdefault(name, []).append(value)
    headers = {name: tuple(values) for name, values in mutable_headers.items()}
    _validate_framing(headers)
    return int(match.group(1)), headers


def _validate_framing(headers: dict[str, tuple[str, ...]]) -> None:
    content_lengths = headers.get("content-length", ())
    transfer_encodings = headers.get("transfer-encoding", ())
    if content_lengths and transfer_encodings:
        raise WebFetchError("invalid_response", "ambiguous HTTP response framing")
    if content_lengths:
        if len(set(content_lengths)) != 1 or not content_lengths[0].isdigit():
            raise WebFetchError("invalid_response", "invalid Content-Length")
    if transfer_encodings:
        combined = ",".join(transfer_encodings).strip().lower()
        if combined != "chunked":
            raise WebFetchError("unsupported_encoding", "unsupported transfer encoding")


async def _read_body(
    reader: asyncio.StreamReader,
    headers: dict[str, tuple[str, ...]],
    max_bytes: int,
) -> bytes:
    content_encoding = ",".join(headers.get("content-encoding", ())).strip().lower()
    if content_encoding not in {"", "identity"}:
        raise WebFetchError("unsupported_encoding", "compressed responses are not accepted")
    content_lengths = headers.get("content-length", ())
    if content_lengths:
        length = int(content_lengths[0])
        if length > max_bytes:
            raise WebFetchError("too_large", "response exceeds the byte limit")
        return await reader.readexactly(length)
    if headers.get("transfer-encoding"):
        return await _read_chunked(reader, max_bytes)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await reader.read(min(65_536, max_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise WebFetchError("too_large", "response exceeds the byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_chunked(reader: asyncio.StreamReader, max_bytes: int) -> bytes:
    body = bytearray()
    while True:
        line = await reader.readline()
        if not line.endswith(b"\r\n") or len(line) > CHUNK_LINE_LIMIT:
            raise WebFetchError("invalid_response", "chunk header is malformed")
        size_text = line[:-2].split(b";", 1)[0]
        try:
            size = int(size_text, 16)
        except ValueError as exc:
            raise WebFetchError("invalid_response", "chunk size is invalid") from exc
        if size < 0 or len(body) + size > max_bytes:
            raise WebFetchError("too_large", "chunked response exceeds the byte limit")
        if size == 0:
            await _read_trailers(reader)
            return bytes(body)
        body.extend(await reader.readexactly(size))
        if await reader.readexactly(2) != b"\r\n":
            raise WebFetchError("invalid_response", "chunk terminator is invalid")


async def _read_trailers(reader: asyncio.StreamReader) -> None:
    total = 0
    while True:
        line = await reader.readline()
        total += len(line)
        if total > HEADER_LIMIT or not line.endswith(b"\r\n"):
            raise WebFetchError("invalid_response", "chunk trailers are invalid")
        if line == b"\r\n":
            return
