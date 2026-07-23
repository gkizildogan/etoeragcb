#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import math
import ssl
import statistics
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class Observation:
    status: int
    duration_ms: float
    done_event: bool = True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded LAN API load smoke")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--ca-file", type=Path, required=True)
    parser.add_argument("--mode", choices=["health", "chat"], default="health")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--email")
    parser.add_argument("--output", type=Path)
    return parser


async def main_async(args: argparse.Namespace) -> int:
    if args.requests < 1 or args.requests > 1000:
        raise SystemExit("--requests must be between 1 and 1000")
    if args.concurrency < 1 or args.concurrency > 32:
        raise SystemExit("--concurrency must be between 1 and 32")
    if args.mode == "chat" and (args.requests > 5 or args.concurrency > 2):
        raise SystemExit("chat smoke is capped at 5 requests and concurrency 2")
    context = ssl.create_default_context(cafile=str(args.ca_file))
    base_url = args.base_url.rstrip("/")
    headers: dict[str, str] = {}
    session_ids: list[str] = []
    if args.mode == "chat":
        if not args.email:
            raise SystemExit("--email is required in chat mode")
        password = getpass.getpass("Load-test account password: ")
        login = await asyncio.to_thread(
            request_json,
            f"{base_url}/api/auth/login",
            context,
            {"email": args.email, "password": password},
            {},
        )
        access_token = login.get("access_token")
        if not isinstance(access_token, str):
            raise SystemExit("login did not return an access token")
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Origin": base_url,
        }
        for index in range(args.requests):
            created = await asyncio.to_thread(
                request_json,
                f"{base_url}/api/sessions",
                context,
                {"title": f"P11 load smoke {index + 1}"},
                headers,
            )
            session_id = created.get("id")
            if not isinstance(session_id, str):
                raise SystemExit("session creation did not return an ID")
            session_ids.append(session_id)

    semaphore = asyncio.Semaphore(args.concurrency)

    async def execute(index: int) -> Observation:
        async with semaphore:
            if args.mode == "health":
                return await asyncio.to_thread(
                    request_raw,
                    f"{base_url}/api/healthz",
                    context,
                    None,
                    {},
                )
            body = {
                "session_id": session_ids[index],
                "message": "Reply briefly: what evidence is available in the indexed documents?",
                "collection_ids": [],
                "document_ids": [],
                "web_search": False,
                "client_request_id": str(uuid.uuid4()),
            }
            request_headers = {
                **headers,
                "Idempotency-Key": f"p11-load-{uuid.uuid4()}",
            }
            return await asyncio.to_thread(
                request_raw,
                f"{base_url}/api/chat",
                context,
                body,
                request_headers,
            )

    started = time.perf_counter()
    observations = await asyncio.gather(*(execute(index) for index in range(args.requests)))
    wall_seconds = time.perf_counter() - started
    durations = sorted(item.duration_ms for item in observations)
    failures = [
        item
        for item in observations
        if item.status < 200 or item.status >= 300 or not item.done_event
    ]
    report: dict[str, Any] = {
        "schema_version": 1,
        "mode": args.mode,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "failures": len(failures),
        "wall_seconds": round(wall_seconds, 3),
        "requests_per_second": round(args.requests / wall_seconds, 3),
        "latency_ms": {
            "mean": round(statistics.fmean(durations), 3),
            "p50": round(percentile(durations, 0.50), 3),
            "p95": round(percentile(durations, 0.95), 3),
            "max": round(max(durations), 3),
        },
        "status_counts": {
            str(status): sum(item.status == status for item in observations)
            for status in sorted({item.status for item in observations})
        },
        "result": "pass" if not failures else "fail",
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{rendered}\n", encoding="utf-8")
    return int(bool(failures))


def request_json(
    url: str,
    context: ssl.SSLContext,
    body: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    observation, content = _request(url, context, body, headers)
    if observation.status < 200 or observation.status >= 300:
        raise RuntimeError(f"request failed with HTTP {observation.status}")
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise RuntimeError("API returned a non-object JSON response")
    return payload


def request_raw(
    url: str,
    context: ssl.SSLContext,
    body: dict[str, Any] | None,
    headers: dict[str, str],
) -> Observation:
    observation, _content = _request(url, context, body, headers)
    return observation


def _request(
    url: str,
    context: ssl.SSLContext,
    body: dict[str, Any] | None,
    headers: dict[str, str],
) -> tuple[Observation, bytes]:
    encoded = json.dumps(body).encode() if body is not None else None
    request = Request(
        url,
        data=encoded,
        method="POST" if body is not None else "GET",
        headers={
            **headers,
            **({"Content-Type": "application/json"} if encoded is not None else {}),
        },
    )
    started = time.perf_counter()
    try:
        with urlopen(request, context=context, timeout=180) as response:
            content = response.read()
            status = response.status
    except HTTPError as exc:
        content = exc.read()
        status = exc.code
    except URLError:
        return Observation(status=0, duration_ms=(time.perf_counter() - started) * 1000), b""
    duration_ms = (time.perf_counter() - started) * 1000
    done = body is None or b"event: done" in content
    return Observation(status=status, duration_ms=duration_ms, done_event=done), content


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    index = max(0, min(len(values) - 1, math.ceil(len(values) * fraction) - 1))
    return values[index]


def main() -> int:
    return asyncio.run(main_async(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
