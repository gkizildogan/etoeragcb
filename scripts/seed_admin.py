#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    command = [
        "docker",
        "compose",
        "--env-file",
        str(ROOT / "deploy" / ".env"),
        "-f",
        str(ROOT / "deploy" / "compose.yml"),
        "exec",
        "backend",
        "python",
        "-m",
        "app.admin_cli",
        "bootstrap-admin",
        *sys.argv[1:],
    ]
    return subprocess.call(command, cwd=ROOT)  # noqa: S603 - fixed executable, intentional args


if __name__ == "__main__":
    raise SystemExit(main())
