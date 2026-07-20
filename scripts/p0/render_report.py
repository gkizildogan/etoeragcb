#!/usr/bin/env python3
"""Render collected P0 evidence without turning missing reboot proof into a pass."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def result_map(evidence: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not evidence:
        return {}
    return {
        item["name"]: item
        for item in evidence.get("results", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def measurement(
    results: dict[str, dict[str, Any]], test: str, field: str, default: Any = "not measured"
) -> Any:
    return results.get(test, {}).get("measurements", {}).get(field, default)


def yes_no(value: bool) -> str:
    return "pass" if value else "missing/fail"


def markdown_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def latest_file(artifacts: Path, pattern: str) -> Path | None:
    paths = sorted(artifacts.glob(pattern))
    return paths[-1] if paths else None


def gpu_snapshot(artifacts: Path) -> dict[str, str]:
    path = latest_file(artifacts, "host/*-after-qualification.gpu.csv")
    if path is None:
        return {}
    with path.open(encoding="utf-8", newline="") as handle:
        row = next(csv.reader(handle), None)
    if row is None or len(row) < 8:
        return {}
    keys = (
        "name",
        "uuid",
        "driver",
        "memory_total_mib",
        "memory_used_mib",
        "memory_free_mib",
        "temperature_c",
        "power_w",
    )
    return {key: value.strip() for key, value in zip(keys, row, strict=True)}


def container_snapshot(artifacts: Path) -> list[dict[str, Any]]:
    path = latest_file(artifacts, "host/*-after-qualification.docker-stats.jsonl")
    if path is None:
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def render(artifacts: Path) -> tuple[str, bool]:
    qualification = read_json(artifacts / "qualification-latest.json")
    restart = read_json(artifacts / "restart-latest.json")
    post_reboot = read_json(artifacts / "post-reboot.json")
    verification = read_env(artifacts / "verification.env")
    pre_reboot_env = read_env(artifacts / "pre-reboot.env")
    post_reboot_env = read_env(artifacts / "post-reboot.env")
    runtime_logs = read_env(artifacts / "runtime-log-verification.env")

    full_passed = bool(qualification and qualification.get("suite") == "full" and qualification.get("passed"))
    restart_passed = bool(restart and restart.get("suite") == "smoke" and restart.get("passed"))
    reboot_ids_changed = bool(
        pre_reboot_env.get("boot_id")
        and post_reboot_env.get("current_boot_id")
        and pre_reboot_env["boot_id"] != post_reboot_env["current_boot_id"]
    )
    verification_passed = bool(
        verification.get("vllm_image", "").find("@sha256:") > 0
        and verification.get("tei_image", "").find("@sha256:") > 0
        and len(verification.get("vllm_revision", "")) == 40
        and len(verification.get("embed_revision", "")) == 40
        and len(verification.get("rerank_revision", "")) == 40
    )
    reboot_evidence_matches_pins = bool(
        pre_reboot_env.get("vllm_revision") == verification.get("vllm_revision")
        and pre_reboot_env.get("embed_revision") == verification.get("embed_revision")
        and pre_reboot_env.get("rerank_revision") == verification.get("rerank_revision")
        and post_reboot_env.get("captured_at", "") > pre_reboot_env.get("captured_at", "")
    )
    reboot_passed = bool(
        post_reboot
        and post_reboot.get("passed")
        and reboot_ids_changed
        and reboot_evidence_matches_pins
    )
    runtime_load_passed = bool(
        runtime_logs.get("compressed_tensors_log_match") == "pass"
        and runtime_logs.get("marlin_log_match") == "pass"
        and runtime_logs.get("oom_scan") == "pass"
    )
    gate_passed = (
        verification_passed
        and runtime_load_passed
        and full_passed
        and restart_passed
        and reboot_passed
    )
    status = "PASS" if gate_passed else "INCOMPLETE / FAILING"

    results = result_map(qualification)
    streaming = results.get("content_only_streaming", {}).get("measurements", {})
    context = results.get("tokenizer_context_ladder", {}).get("measurements", {})
    concurrency = results.get("measured_concurrency", {}).get("measurements", {})
    embed_quality = results.get("embedding_bilingual_quality", {}).get("measurements", {})
    embed_throughput = results.get("embedding_throughput", {}).get("measurements", {})
    rerank = results.get("reranker_bilingual_quality_latency", {}).get("measurements", {})
    startup = results.get("startup_readiness", {}).get("measurements", {}).get("ready_wait_seconds", {})
    gpu = gpu_snapshot(artifacts)
    containers = container_snapshot(artifacts)

    lines = [
        "# P0 qualification evidence",
        "",
        f"**Gate status: {status}.**",
        "",
        "This report is generated from `artifacts/p0/`. P1 must not begin unless all five gate rows pass.",
        "",
        "## Gate evidence",
        "",
        "| Evidence | Status |",
        "|---|---|",
        f"| Immutable images and exact local revisions | {yes_no(verification_passed)} |",
        f"| compressed-tensors/Marlin load and no-OOM log scan | {yes_no(runtime_load_passed)} |",
        f"| Full qualification suite | {yes_no(full_passed)} |",
        f"| Container restart smoke suite | {yes_no(restart_passed)} |",
        f"| Changed boot ID plus post-reboot smoke suite | {yes_no(reboot_passed)} |",
        "",
        "## Pins",
        "",
        "| Item | Value |",
        "|---|---|",
        f"| vLLM image | `{markdown_escape(verification.get('vllm_image', 'missing'))}` |",
        f"| TEI image | `{markdown_escape(verification.get('tei_image', 'missing'))}` |",
        f"| Generation model | `{markdown_escape(verification.get('vllm_model', 'missing'))}` |",
        f"| Generation revision | `{markdown_escape(verification.get('vllm_revision', 'missing'))}` |",
        f"| Embedding model | `{markdown_escape(verification.get('embed_model', 'missing'))}` |",
        f"| Embedding revision | `{markdown_escape(verification.get('embed_revision', 'missing'))}` |",
        f"| Reranker model | `{markdown_escape(verification.get('rerank_model', 'missing'))}` |",
        f"| Reranker revision | `{markdown_escape(verification.get('rerank_revision', 'missing'))}` |",
        "",
        "## Measurements",
        "",
        "| Measurement | Result |",
        "|---|---:|",
        f"| vLLM readiness wait | {markdown_escape(startup.get('vllm', 'not measured'))} s |",
        f"| Streaming time to first content | {markdown_escape(streaming.get('time_to_first_content_seconds', 'not measured'))} s |",
        f"| Decode rate after first content | {markdown_escape(streaming.get('decode_tokens_per_second_after_first', 'not measured'))} token/s |",
        f"| Largest stable prompt | {markdown_escape(context.get('largest_stable_prompt_tokens', 'not measured'))} tokens |",
        f"| Largest measured concurrency | {markdown_escape(concurrency.get('largest_successful_level', 'not measured'))} |",
        f"| Embedding dimension | {markdown_escape(embed_quality.get('dimension', 'not measured'))} |",
        f"| Embedding batch throughput | {markdown_escape(embed_throughput.get('items_per_second', 'not measured'))} item/s |",
        f"| Rerank median latency | {markdown_escape(rerank.get('median_latency_seconds', 'not measured'))} s |",
        f"| Rerank p95 latency | {markdown_escape(rerank.get('p95_latency_seconds', 'not measured'))} s |",
        f"| GPU memory after qualification | {markdown_escape(gpu.get('memory_used_mib', 'not measured'))} / {markdown_escape(gpu.get('memory_total_mib', 'not measured'))} MiB |",
        "",
        "### Container resource snapshot",
        "",
        "| Container | CPU | Memory |",
        "|---|---:|---:|",
    ]
    if containers:
        for container in containers:
            lines.append(
                f"| {markdown_escape(container.get('Name', container.get('Container', 'unknown')))} "
                f"| {markdown_escape(container.get('CPUPerc', ''))} "
                f"| {markdown_escape(container.get('MemUsage', ''))} |"
            )
    else:
        lines.append("| not measured |  |  |")
    lines.extend(
        [
        "",
        "## Test details",
        "",
        "| Test | Status | Duration (s) | Error |",
        "|---|---|---:|---|",
        ]
    )
    if qualification:
        for item in qualification.get("results", []):
            lines.append(
                "| {name} | {status} | {duration} | {error} |".format(
                    name=markdown_escape(item.get("name", "unknown")),
                    status="pass" if item.get("passed") else "fail",
                    duration=markdown_escape(item.get("duration_seconds", "")),
                    error=markdown_escape(item.get("error", "")),
                )
            )
    else:
        lines.append("| full qualification | missing |  | Run `./scripts/p0/run.sh all` |")

    lines.extend(
        [
            "",
            "## Review notes",
            "",
            "- Inspect host GPU/RAM snapshots in `artifacts/p0/host/` before accepting limits.",
            "- Inspect vLLM logs for `compressed-tensors` plus the expected Marlin W4A16 path and for any OOM/reasoning leakage.",
            "- Copy reviewed measurements and final serving limits into `docs/feasibility.md`.",
            "- A generated report is evidence, not a substitute for reviewing smoke quality and resource headroom.",
            "",
        ]
    )
    return "\n".join(lines), gate_passed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report, gate_passed = render(args.artifacts)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"P0 gate: {'PASS' if gate_passed else 'INCOMPLETE / FAILING'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
