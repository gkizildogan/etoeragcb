#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def main() -> int:
    parser = argparse.ArgumentParser(description="Atomically write a non-secret backup marker")
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--backup-id", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--off-machine-uploaded", action="store_true")
    args = parser.parse_args()
    parsed = urlsplit(args.destination)
    if parsed.scheme not in {"gdrive", "s3", "sftp"}:
        raise SystemExit("unsupported backup destination scheme")
    payload = {
        "schema_version": 1,
        "backup_id": args.backup_id,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "encrypted": True,
        "repository_checked": True,
        "off_machine_uploaded": args.off_machine_uploaded,
        "destination_scheme": parsed.scheme,
        "restic_snapshot_id": args.snapshot_id,
    }
    args.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = args.path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, args.path)
    finally:
        temporary.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
