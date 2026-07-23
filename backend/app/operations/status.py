from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError


class BackupStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int
    backup_id: str
    completed_at: datetime
    encrypted: bool
    repository_checked: bool
    off_machine_uploaded: bool
    destination_scheme: str
    restic_snapshot_id: str

    @property
    def is_verified_off_machine(self) -> bool:
        return (
            self.schema_version == 1
            and self.encrypted
            and self.repository_checked
            and self.off_machine_uploaded
            and self.completed_at.tzinfo is not None
        )


def read_backup_status(path: Path) -> BackupStatus | None:
    try:
        if path.stat().st_size > 64 * 1024:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        status = BackupStatus.model_validate(payload)
    except (OSError, ValueError, ValidationError):
        return None
    return status.model_copy(update={"completed_at": status.completed_at.astimezone(UTC)})


def has_recent_off_machine_backup(
    path: Path,
    *,
    max_age_hours: int,
    now: datetime | None = None,
) -> bool:
    status = read_backup_status(path)
    if status is None or not status.is_verified_off_machine:
        return False
    checked_at = now or datetime.now(UTC)
    age = checked_at - status.completed_at
    return 0 <= age.total_seconds() <= max_age_hours * 3600
