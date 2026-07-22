from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import SplitResult, quote, urlsplit, urlunsplit


class WebFetchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ValidatedUrl:
    canonical_url: str
    scheme: str
    host: str
    port: int
    request_target: str
    host_header: str


class DnsResolver(Protocol):
    async def resolve(
        self, host: str, port: int
    ) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]: ...


class SystemDnsResolver:
    async def resolve(
        self, host: str, port: int
    ) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            return (literal,)
        try:
            records = await asyncio.get_running_loop().getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except OSError as exc:
            raise WebFetchError("dns_failure", "DNS resolution failed") from exc
        addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        for family, _type, _proto, _canonical, sockaddr in records:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            try:
                addresses.add(ipaddress.ip_address(str(sockaddr[0])))
            except ValueError:
                continue
        if not addresses:
            raise WebFetchError("dns_failure", "DNS returned no usable address")
        return tuple(sorted(addresses, key=lambda item: (item.version, int(item))))


_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_EXPLICIT_METADATA_ADDRESSES = (
    ipaddress.ip_network("168.63.129.16/32"),
    ipaddress.ip_network("169.254.169.254/32"),
    ipaddress.ip_network("169.254.170.2/32"),
    ipaddress.ip_network("100.100.100.200/32"),
    ipaddress.ip_network("fd00:ec2::254/128"),
)


def validate_url(url: str, allowed_ports: frozenset[int]) -> ValidatedUrl:
    if not url or len(url) > 4096 or _has_forbidden_url_character(url):
        raise WebFetchError("invalid_url", "URL is empty, oversized, or malformed")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise WebFetchError("invalid_url", "URL authority is invalid") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise WebFetchError("invalid_scheme", "only HTTP and HTTPS URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise WebFetchError("credentials_forbidden", "URL credentials are forbidden")
    if parsed.hostname is None:
        raise WebFetchError("invalid_url", "URL hostname is required")
    host = _normalize_host(parsed.hostname)
    resolved_port = port if port is not None else (443 if scheme == "https" else 80)
    if resolved_port not in allowed_ports:
        raise WebFetchError("port_forbidden", "URL port is not allowlisted")
    path = quote(parsed.path or "/", safe="/:@!$&'()*+,;=-._~%")
    query = quote(parsed.query, safe="/?:@!$&'()*+,;=-._~%")
    if not path.startswith("/"):
        raise WebFetchError("invalid_url", "URL path must be absolute")
    request_target = path + (f"?{query}" if query else "")
    host_for_authority = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    authority = (
        host_for_authority
        if resolved_port == default_port
        else f"{host_for_authority}:{resolved_port}"
    )
    canonical = urlunsplit(SplitResult(scheme, authority, path, query, ""))
    return ValidatedUrl(
        canonical_url=canonical,
        scheme=scheme,
        host=host,
        port=resolved_port,
        request_target=request_target,
        host_header=authority,
    )


def validate_public_addresses(
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...],
) -> None:
    if not addresses:
        raise WebFetchError("dns_failure", "DNS returned no addresses")
    for address in addresses:
        if (
            not address.is_global
            or address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
            or any(address in network for network in _EXPLICIT_METADATA_ADDRESSES)
        ):
            raise WebFetchError("forbidden_address", "DNS resolved to a forbidden address")


def _normalize_host(host: str) -> str:
    candidate = host.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        try:
            candidate = candidate.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise WebFetchError("invalid_url", "hostname IDNA encoding failed") from exc
        if len(candidate) > 253 or any(
            _HOST_LABEL.fullmatch(label) is None for label in candidate.split(".")
        ):
            raise WebFetchError("invalid_url", "hostname is malformed") from None
        return candidate
    return address.compressed


def _has_forbidden_url_character(url: str) -> bool:
    return "\\" in url or any(ord(character) < 33 or ord(character) == 127 for character in url)
