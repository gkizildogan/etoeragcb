#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "deploy" / "compose.yml"


def main() -> int:
    command = [
        "docker",
        "compose",
        "--env-file",
        str(ROOT / "deploy" / ".env.example"),
        "-f",
        str(COMPOSE_FILE),
        "config",
        "--format",
        "json",
    ]
    completed = subprocess.run(command, check=True, capture_output=True, text=True)  # noqa: S603
    config: dict[str, Any] = json.loads(completed.stdout)
    violations: list[str] = []
    published: list[tuple[str, int]] = []
    for service_name, service in config["services"].items():
        for port in service.get("ports", []):
            target = int(port["target"])
            published.append((service_name, target))
            if service_name != "caddy" or target not in {80, 443}:
                violations.append(f"{service_name} publishes {target}")

    if sorted(published) != [("caddy", 80), ("caddy", 443)]:
        violations.append(f"expected only caddy 80/443, found {sorted(published)}")
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    print("PASS: only Caddy publishes host ports 80 and 443")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
