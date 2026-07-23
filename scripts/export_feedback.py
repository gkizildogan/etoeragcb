#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export one tenant's RAG feedback to a private JSONL file"
    )
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-content", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not args.overwrite:
        raise RuntimeError("output exists; pass --overwrite to replace it")
    container_output = f"/export/{output.name}"
    command = [
        "docker",
        "compose",
        "--env-file",
        str(ROOT / "deploy" / ".env"),
        "-f",
        str(ROOT / "deploy" / "compose.yml"),
        "run",
        "--rm",
        "--no-deps",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--volume",
        f"{output.parent}:/export",
        "backend",
        "python",
        "-m",
        "app.evaluation.cli",
        "export-feedback",
        "--tenant-id",
        args.tenant_id,
        "--output",
        container_output,
    ]
    if args.include_content:
        command.append("--include-content")
    if args.overwrite:
        command.append("--overwrite")
    return subprocess.call(command, cwd=ROOT)  # noqa: S603 - fixed executable and arguments


if __name__ == "__main__":
    raise SystemExit(main())
