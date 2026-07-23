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
        "--profile",
        "*",
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

    service_networks = {
        service_name: set(service.get("networks", {}))
        for service_name, service in config["services"].items()
    }
    fetch_egress_users = sorted(
        name for name, networks in service_networks.items() if "fetch_egress" in networks
    )
    if fetch_egress_users != ["web-fetcher"]:
        violations.append(
            f"expected only web-fetcher on fetch_egress, found {fetch_egress_users}"
        )
    if service_networks.get("web-fetcher") != {"search", "fetch_egress"}:
        violations.append(
            "web-fetcher must connect only to private search and fetch_egress networks"
        )
    if "fetch_egress" in service_networks.get("backend", set()):
        violations.append("backend must not have direct page-fetch egress")
    backup_egress_users = sorted(
        name for name, networks in service_networks.items() if "backup_egress" in networks
    )
    expected_backup_egress = [
        "backup-rclone",
        "backup-rclone-configure",
        "backup-rclone-restore",
    ]
    if backup_egress_users != expected_backup_egress:
        violations.append(
            "backup_egress must be limited to the rclone transfer/configuration services; "
            f"found {backup_egress_users}"
        )
    for service_name in expected_backup_egress:
        if service_networks.get(service_name) != {"backup_egress"}:
            violations.append(f"{service_name} must connect only to backup_egress")
    alert_egress_users = sorted(
        name for name, networks in service_networks.items() if "alert_egress" in networks
    )
    if alert_egress_users != ["alertmanager"]:
        violations.append(
            f"expected only alertmanager on alert_egress, found {alert_egress_users}"
        )
    if service_networks.get("alertmanager") != {"edge", "alert_egress"}:
        violations.append("alertmanager must connect only to edge and alert_egress")
    for network_name in ("edge", "data", "model", "search"):
        if not config["networks"].get(network_name, {}).get("internal", False):
            violations.append(f"{network_name} must remain an internal network")
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    print(
        "PASS: only Caddy publishes ports; only web-fetcher has page egress; "
        "only rclone has backup egress; only Alertmanager has SMTP egress; "
        "application networks remain internal"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
