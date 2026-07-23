#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from typing import Any


def main() -> int:
    payload: Any = json.load(sys.stdin)
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise SystemExit("expected exactly one latest restic snapshot")
    snapshot_id = payload[0].get("id")
    if not isinstance(snapshot_id, str) or len(snapshot_id) < 16:
        raise SystemExit("restic did not return a valid snapshot ID")
    print(snapshot_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
