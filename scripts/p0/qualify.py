#!/usr/bin/env python3
"""Qualify the pinned P0 model servers using only the Python standard library."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import platform
import re
import statistics
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


THINK_MARKERS = ("<think>", "</think>", "<|channel|>analysis", "reasoning_content")
PLANNER_FIELDS = {
    "intent",
    "query",
    "exact_terms",
    "document_hints",
    "collection_hints",
    "heading_hints",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def env_int_list(name: str, default: str) -> list[int]:
    values = [int(item.strip()) for item in os.getenv(name, default).split(",")]
    if not values or any(value <= 0 for value in values):
        raise ValueError(f"{name} must contain positive comma-separated integers")
    return values


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("Cannot calculate a percentile of no values")
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        raise AssertionError("Embedding vector has zero norm")
    return numerator / (left_norm * right_norm)


class ProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpClient:
    base_url: str
    api_key: str
    timeout: float

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> tuple[Any, dict[str, str]]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                response_body = response.read()
                response_headers = {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise ProbeError(f"HTTP {exc.code} from {path}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProbeError(f"Request to {path} failed: {exc}") from exc
        if not response_body:
            return None, response_headers
        try:
            return json.loads(response_body), response_headers
        except json.JSONDecodeError as exc:
            raise ProbeError(f"Non-JSON response from {path}") from exc

    def stream(self, path: str, payload: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise ProbeError(f"HTTP {exc.code} from {path}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProbeError(f"Streaming request to {path} failed: {exc}") from exc


@dataclass
class Qualification:
    suite: str
    output_path: Path
    vllm: HttpClient
    embed: HttpClient
    rerank: HttpClient
    model: str
    model_revision: str
    embed_model: str
    embed_revision: str
    rerank_model: str
    rerank_revision: str
    startup_timeout: int
    context_steps: list[int]
    concurrency_levels: list[int]
    concurrency_output_tokens: int
    embed_batch_size: int
    rerank_repetitions: int
    results: list[dict[str, Any]] = field(default_factory=list)

    def record(self, name: str, function: Callable[[], dict[str, Any]]) -> None:
        started = time.perf_counter()
        try:
            measurements = function()
            result = {
                "name": name,
                "passed": True,
                "duration_seconds": round(time.perf_counter() - started, 4),
                "measurements": measurements,
            }
            print(f"PASS {name} ({result['duration_seconds']:.4f}s)", flush=True)
        except Exception as exc:  # Continue to preserve complete evidence.
            result = {
                "name": name,
                "passed": False,
                "duration_seconds": round(time.perf_counter() - started, 4),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(limit=8),
            }
            print(f"FAIL {name}: {exc}", file=sys.stderr, flush=True)
        self.results.append(result)

    def wait_for(self, name: str, client: HttpClient) -> float:
        started = time.perf_counter()
        deadline = started + self.startup_timeout
        last_error = "not attempted"
        while time.perf_counter() < deadline:
            try:
                client.request("GET", "/health", timeout=5)
                return time.perf_counter() - started
            except Exception as exc:
                last_error = str(exc)
                time.sleep(2)
        raise ProbeError(f"{name} was not ready within {self.startup_timeout}s: {last_error}")

    def startup(self) -> dict[str, Any]:
        latencies = {
            "vllm": round(self.wait_for("vLLM", self.vllm), 4),
            "tei_embed": round(self.wait_for("TEI embed", self.embed), 4),
            "tei_rerank": round(self.wait_for("TEI rerank", self.rerank), 4),
        }
        return {"ready_wait_seconds": latencies}

    def identities(self) -> dict[str, Any]:
        models, _ = self.vllm.request("GET", "/v1/models")
        served_ids = [item["id"] for item in models.get("data", [])]
        if self.model not in served_ids:
            raise AssertionError(f"Expected served model {self.model!r}; got {served_ids!r}")
        version, _ = self.vllm.request("GET", "/version")
        return {
            "served_model_ids": served_ids,
            "vllm_version": version.get("version") if isinstance(version, dict) else version,
            "expected_model_revision": self.model_revision,
            "expected_embed_revision": self.embed_revision,
            "expected_rerank_revision": self.rerank_revision,
        }

    def chat_payload(self, messages: list[dict[str, str]], max_tokens: int = 128) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }

    def chat(self, messages: list[dict[str, str]], max_tokens: int = 128) -> tuple[str, Any, dict[str, Any]]:
        response, _ = self.vllm.request(
            "POST", "/v1/chat/completions", self.chat_payload(messages, max_tokens)
        )
        message = response["choices"][0]["message"]
        content = message.get("content") or ""
        reasoning = message.get("reasoning")
        if reasoning is None:
            reasoning = message.get("reasoning_content")
        self.assert_no_reasoning(content, reasoning)
        return content, reasoning, response

    @staticmethod
    def assert_no_reasoning(content: str, reasoning: Any) -> None:
        if reasoning not in (None, "", []):
            raise AssertionError("Reasoning field was not empty with thinking disabled")
        lowered = content.casefold()
        leaked = [marker for marker in THINK_MARKERS if marker.casefold() in lowered]
        if leaked:
            raise AssertionError(f"Reasoning marker leaked into content: {leaked}")

    def bilingual(self) -> dict[str, Any]:
        english, english_reasoning, english_response = self.chat(
            [
                {"role": "system", "content": "Follow the response-format instruction exactly."},
                {"role": "user", "content": "Reply with exactly: Ready."},
            ],
            16,
        )
        turkish, turkish_reasoning, turkish_response = self.chat(
            [
                {"role": "system", "content": "Yanıt biçimi talimatına harfiyen uy."},
                {"role": "user", "content": "Yalnızca tam olarak şunu yaz: Hazırım."},
            ],
            16,
        )
        if english.strip() != "Ready.":
            raise AssertionError(f"Unexpected English exact response: {english!r}")
        if turkish.strip() != "Hazırım.":
            raise AssertionError(f"Unexpected Turkish exact response: {turkish!r}")
        return {
            "english": english.strip(),
            "turkish": turkish.strip(),
            "reasoning_fields_empty": english_reasoning is None and turkish_reasoning is None,
            "english_usage": english_response.get("usage", {}),
            "turkish_usage": turkish_response.get("usage", {}),
        }

    def planner(self) -> dict[str, Any]:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "intent": {"type": "string", "enum": ["smalltalk", "meta", "knowledge"]},
                "query": {"type": "string", "minLength": 1, "maxLength": 512},
                "exact_terms": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                "document_hints": {
                    "type": "array",
                    "maxItems": 5,
                    "items": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                "collection_hints": {
                    "type": "array",
                    "maxItems": 5,
                    "items": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                "heading_hints": {
                    "type": "array",
                    "maxItems": 5,
                    "items": {"type": "string", "minLength": 1, "maxLength": 128},
                },
            },
            "required": sorted(PLANNER_FIELDS),
        }
        payload = self.chat_payload(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a bounded retrieval planner. Return only the six requested JSON "
                        "fields. Use the user's language. Hints are text only and never filters."
                    ),
                },
                {
                    "role": "user",
                    "content": "İK El Kitabı'nın İzinler bölümünde yıllık izin politikası nedir?",
                },
            ],
            256,
        )
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "retrieval_plan", "strict": True, "schema": schema},
        }
        response, _ = self.vllm.request("POST", "/v1/chat/completions", payload)
        message = response["choices"][0]["message"]
        content = message.get("content") or ""
        reasoning = message.get("reasoning", message.get("reasoning_content"))
        self.assert_no_reasoning(content, reasoning)
        plan = json.loads(content)
        if set(plan) != PLANNER_FIELDS:
            raise AssertionError(f"Planner fields differ: {set(plan)!r}")
        if plan["intent"] != "knowledge":
            raise AssertionError(f"Planner intent was {plan['intent']!r}")
        if not plan["query"].strip():
            raise AssertionError("Planner query was empty")
        for field_name in PLANNER_FIELDS - {"intent", "query"}:
            if not isinstance(plan[field_name], list):
                raise AssertionError(f"Planner {field_name} was not a list")
        return {
            "valid_json": True,
            "fields": sorted(plan),
            "intent": plan["intent"],
            "query_language_smoke": "Turkish",
            "reasoning_field_empty": reasoning is None,
            "usage": response.get("usage", {}),
        }

    def citation(self) -> dict[str, Any]:
        content, reasoning, response = self.chat(
            [
                {
                    "role": "system",
                    "content": "Use only the supplied source and cite it with its exact marker.",
                },
                {
                    "role": "user",
                    "content": "Source [S1]: Turkey's capital is Ankara. Answer: What is the capital?",
                },
            ],
            32,
        )
        markers = re.findall(r"\[S\d+\]", content)
        if markers != ["[S1]"] or "ankara" not in content.casefold():
            raise AssertionError(f"Citation behavior failed: {content!r}")
        return {
            "content": content.strip(),
            "markers": markers,
            "reasoning_field_empty": reasoning is None,
            "usage": response.get("usage", {}),
        }

    def streaming(self) -> dict[str, Any]:
        payload = self.chat_payload(
            [
                {
                    "role": "user",
                    "content": "In English, give a concise four-sentence explanation of hybrid retrieval.",
                }
            ],
            160,
        )
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        started = time.perf_counter()
        first_content_at: float | None = None
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: dict[str, Any] = {}
        with self.vllm.stream("/v1/chat/completions", payload) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="strict").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                event = json.loads(data)
                if event.get("usage"):
                    usage = event["usage"]
                for choice in event.get("choices", []):
                    delta = choice.get("delta", {})
                    content_delta = delta.get("content") or ""
                    reasoning_delta = delta.get("reasoning") or delta.get("reasoning_content") or ""
                    if content_delta and first_content_at is None:
                        first_content_at = time.perf_counter()
                    content_parts.append(content_delta)
                    reasoning_parts.append(reasoning_delta)
        finished = time.perf_counter()
        if first_content_at is None:
            raise AssertionError("Stream had no content delta")
        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        self.assert_no_reasoning(content, reasoning)
        completion_tokens = int(usage.get("completion_tokens", 0))
        decode_window = max(finished - first_content_at, 1e-9)
        decode_rate = max(completion_tokens - 1, 0) / decode_window
        return {
            "time_to_first_content_seconds": round(first_content_at - started, 4),
            "stream_duration_seconds": round(finished - started, 4),
            "completion_tokens": completion_tokens,
            "decode_tokens_per_second_after_first": round(decode_rate, 4),
            "content_characters": len(content),
            "reasoning_characters": len(reasoning),
            "usage": usage,
        }

    def context_ladder(self) -> dict[str, Any]:
        largest = max(self.context_steps)
        seed = "Bağlam ölçümü için güvenli Türkçe ve English metin. "
        prompt = seed * max(512, largest // 4)
        tokenized, _ = self.vllm.request(
            "POST",
            "/tokenize",
            {"model": self.model, "prompt": prompt, "add_special_tokens": False},
        )
        tokens = tokenized["tokens"]
        if len(tokens) < largest:
            raise AssertionError(f"Generated only {len(tokens)} context tokens; need {largest}")
        measurements: list[dict[str, Any]] = []
        for target in self.context_steps:
            started = time.perf_counter()
            response, _ = self.vllm.request(
                "POST",
                "/v1/completions",
                {
                    "model": self.model,
                    "prompt": tokens[:target],
                    "temperature": 0,
                    "max_tokens": 8,
                },
            )
            elapsed = time.perf_counter() - started
            actual = int(response.get("usage", {}).get("prompt_tokens", -1))
            if actual != target:
                raise AssertionError(f"Tokenizer/server mismatch at {target}: usage reported {actual}")
            measurements.append(
                {
                    "target_prompt_tokens": target,
                    "actual_prompt_tokens": actual,
                    "completion_tokens": response.get("usage", {}).get("completion_tokens"),
                    "duration_seconds": round(elapsed, 4),
                }
            )
        return {"steps": measurements, "largest_stable_prompt_tokens": measurements[-1]["actual_prompt_tokens"]}

    def concurrency(self) -> dict[str, Any]:
        level_results: list[dict[str, Any]] = []
        for level in self.concurrency_levels:
            barrier = threading.Barrier(level)

            def one_request(index: int) -> dict[str, Any]:
                barrier.wait(timeout=30)
                started = time.perf_counter()
                content, reasoning, response = self.chat(
                    [
                        {
                            "role": "user",
                            "content": (
                                f"Request {index}: explain dense and sparse retrieval differences in "
                                "about 80 English words."
                            ),
                        }
                    ],
                    self.concurrency_output_tokens,
                )
                return {
                    "duration_seconds": time.perf_counter() - started,
                    "completion_tokens": int(response.get("usage", {}).get("completion_tokens", 0)),
                    "content_characters": len(content),
                    "reasoning_empty": reasoning is None,
                }

            batch_started = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=level) as executor:
                futures = [executor.submit(one_request, index) for index in range(level)]
                completed = [future.result() for future in futures]
            batch_elapsed = time.perf_counter() - batch_started
            if len(completed) != level or any(item["content_characters"] == 0 for item in completed):
                raise AssertionError(f"Concurrency level {level} did not complete every response")
            total_tokens = sum(item["completion_tokens"] for item in completed)
            level_results.append(
                {
                    "level": level,
                    "batch_duration_seconds": round(batch_elapsed, 4),
                    "request_duration_seconds": [round(item["duration_seconds"], 4) for item in completed],
                    "completion_tokens": total_tokens,
                    "aggregate_completion_tokens_per_second": round(total_tokens / batch_elapsed, 4),
                }
            )
        return {"levels": level_results, "largest_successful_level": level_results[-1]["level"]}

    def embed_quality(self) -> dict[str, Any]:
        inputs = [
            "What is the capital of Turkey?",
            "Ankara is the capital city of Turkey.",
            "Whales are large marine mammals.",
            "Türkiye'nin başkenti neresidir?",
            "Türkiye'nin başkenti Ankara'dır.",
            "Balinalar büyük deniz memelileridir.",
        ]
        vectors, _ = self.embed.request("POST", "/embed", {"inputs": inputs})
        if len(vectors) != len(inputs):
            raise AssertionError(f"Expected {len(inputs)} vectors, got {len(vectors)}")
        dimensions = {len(vector) for vector in vectors}
        if dimensions != {1024}:
            raise AssertionError(f"Expected embedding dimension 1024, got {dimensions}")
        english_relevant = cosine(vectors[0], vectors[1])
        english_irrelevant = cosine(vectors[0], vectors[2])
        turkish_relevant = cosine(vectors[3], vectors[4])
        turkish_irrelevant = cosine(vectors[3], vectors[5])
        cross_lingual = cosine(vectors[0], vectors[4])
        if english_relevant <= english_irrelevant:
            raise AssertionError("English relevant embedding did not outrank irrelevant text")
        if turkish_relevant <= turkish_irrelevant:
            raise AssertionError("Turkish relevant embedding did not outrank irrelevant text")
        return {
            "dimension": dimensions.pop(),
            "english_relevant_cosine": round(english_relevant, 6),
            "english_irrelevant_cosine": round(english_irrelevant, 6),
            "turkish_relevant_cosine": round(turkish_relevant, 6),
            "turkish_irrelevant_cosine": round(turkish_irrelevant, 6),
            "cross_lingual_cosine": round(cross_lingual, 6),
        }

    def embed_throughput(self) -> dict[str, Any]:
        inputs = [
            f"Bilingual embedding throughput item {index}: yıllık izin ve şirket politikası."
            for index in range(self.embed_batch_size)
        ]
        started = time.perf_counter()
        vectors, _ = self.embed.request("POST", "/embed", {"inputs": inputs})
        elapsed = time.perf_counter() - started
        if len(vectors) != len(inputs):
            raise AssertionError("Embedding throughput batch returned the wrong item count")
        return {
            "batch_size": len(inputs),
            "duration_seconds": round(elapsed, 4),
            "items_per_second": round(len(inputs) / elapsed, 4),
        }

    @staticmethod
    def rerank_results(payload: Any) -> list[dict[str, Any]]:
        results = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(results, list) or not results:
            raise AssertionError(f"Unexpected rerank response shape: {type(payload).__name__}")
        return results

    def rerank_once(self, query: str, texts: list[str]) -> tuple[list[dict[str, Any]], float]:
        started = time.perf_counter()
        payload, _ = self.rerank.request(
            "POST",
            "/rerank",
            {"query": query, "texts": texts, "raw_scores": False, "return_text": False},
        )
        return self.rerank_results(payload), time.perf_counter() - started

    def rerank_quality_latency(self) -> dict[str, Any]:
        english_texts = [
            "Ankara is the capital city of Turkey.",
            "Whales are large marine mammals.",
            "The Pacific Ocean is very large.",
        ]
        turkish_texts = [
            "Türkiye'nin başkenti Ankara'dır.",
            "Balinalar büyük deniz memelileridir.",
            "Pasifik Okyanusu çok büyüktür.",
        ]
        english, first_latency = self.rerank_once("What is the capital of Turkey?", english_texts)
        turkish, second_latency = self.rerank_once("Türkiye'nin başkenti neresidir?", turkish_texts)
        if int(english[0]["index"]) != 0:
            raise AssertionError(f"English reranker chose index {english[0]['index']}")
        if int(turkish[0]["index"]) != 0:
            raise AssertionError(f"Turkish reranker chose index {turkish[0]['index']}")
        latencies = [first_latency, second_latency]
        for _ in range(self.rerank_repetitions):
            _, latency = self.rerank_once("Türkiye'nin başkenti neresidir?", turkish_texts)
            latencies.append(latency)
        return {
            "english_top_index": int(english[0]["index"]),
            "turkish_top_index": int(turkish[0]["index"]),
            "requests": len(latencies),
            "median_latency_seconds": round(statistics.median(latencies), 4),
            "p95_latency_seconds": round(percentile(latencies, 0.95), 4),
            "latency_seconds": [round(value, 4) for value in latencies],
        }

    def run(self) -> bool:
        self.record("startup_readiness", self.startup)
        if not self.results[-1]["passed"]:
            self.write_output()
            return False

        tests: list[tuple[str, Callable[[], dict[str, Any]]]] = [
            ("pinned_service_identity", self.identities),
            ("non_thinking_bilingual_generation", self.bilingual),
            ("schema_constrained_planner", self.planner),
            ("citation_marker_generation", self.citation),
            ("embedding_bilingual_quality", self.embed_quality),
            ("reranker_bilingual_quality_latency", self.rerank_quality_latency),
        ]
        if self.suite == "full":
            tests[4:4] = [
                ("content_only_streaming", self.streaming),
                ("tokenizer_context_ladder", self.context_ladder),
                ("measured_concurrency", self.concurrency),
            ]
            tests.append(("embedding_throughput", self.embed_throughput))
        for name, function in tests:
            self.record(name, function)
        self.write_output()
        return all(result["passed"] for result in self.results)

    def write_output(self) -> None:
        passed = all(result["passed"] for result in self.results)
        output = {
            "schema_version": 1,
            "suite": self.suite,
            "started_at": self.started_at,
            "completed_at": utc_now(),
            "passed": passed,
            "platform": {
                "python": platform.python_version(),
                "system": platform.platform(),
            },
            "pins": {
                "vllm_model": self.model,
                "vllm_model_revision": self.model_revision,
                "embed_model": self.embed_model,
                "embed_revision": self.embed_revision,
                "rerank_model": self.rerank_model,
                "rerank_revision": self.rerank_revision,
            },
            "requested_limits": {
                "context_steps": self.context_steps,
                "concurrency_levels": self.concurrency_levels,
                "concurrency_output_tokens": self.concurrency_output_tokens,
                "embed_batch_size": self.embed_batch_size,
                "rerank_repetitions": self.rerank_repetitions,
            },
            "results": self.results,
        }
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.output_path.with_suffix(self.output_path.suffix + ".tmp")
        temporary.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(self.output_path)

    started_at: str = field(default_factory=utc_now)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("full", "smoke"), default="full")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    timeout = env_int("P0_REQUEST_TIMEOUT_SECONDS", 180)
    vllm_key = os.environ["P0_VLLM_API_KEY"]
    tei_key = os.environ["P0_TEI_API_KEY"]
    qualification = Qualification(
        suite=args.suite,
        output_path=args.output,
        vllm=HttpClient(os.environ["VLLM_BASE_URL"], vllm_key, timeout),
        embed=HttpClient(os.environ["EMBED_URL"], tei_key, timeout),
        rerank=HttpClient(os.environ["RERANK_URL"], tei_key, timeout),
        model=os.environ["VLLM_MODEL"],
        model_revision=os.environ["VLLM_MODEL_REVISION"],
        embed_model=os.environ["EMBED_MODEL"],
        embed_revision=os.environ["EMBED_REVISION"],
        rerank_model=os.environ["RERANK_MODEL"],
        rerank_revision=os.environ["RERANK_REVISION"],
        startup_timeout=env_int("P0_STARTUP_TIMEOUT_SECONDS", 360),
        context_steps=env_int_list("P0_CONTEXT_STEPS", "2048,4096,6144,8000"),
        concurrency_levels=env_int_list("P0_CONCURRENCY_LEVELS", "1,2"),
        concurrency_output_tokens=env_int("P0_CONCURRENCY_OUTPUT_TOKENS", 128),
        embed_batch_size=env_int("P0_EMBED_BATCH_SIZE", 32),
        rerank_repetitions=env_int("P0_RERANK_REPETITIONS", 5),
    )
    return 0 if qualification.run() else 1


if __name__ == "__main__":
    raise SystemExit(main())
