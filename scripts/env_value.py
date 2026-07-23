#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def read_value(path: Path, key: str) -> str | None:
    if KEY_RE.fullmatch(key) is None:
        raise ValueError("invalid environment key")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        candidate, value = line.split("=", maxsplit=1)
        if candidate.strip() == key:
            return value.strip().strip("\"'")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Read one literal value from a Compose env file")
    parser.add_argument("path", type=Path)
    parser.add_argument("key")
    parser.add_argument("--default")
    args = parser.parse_args()
    value = read_value(args.path, args.key)
    if value is None:
        value = args.default
    if value is None:
        raise SystemExit(f"{args.key} is not set in {args.path}")
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
