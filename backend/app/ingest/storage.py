from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePath

from fastapi import UploadFile

from app.ingest.parsers import ParseError, sniff_mime

SAFE_FILENAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,255}$")


class UploadValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StagedUpload:
    temp_path: Path
    source_filename: str
    mime: str
    file_sha256: str
    size_bytes: int


class LocalDocumentStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._staging = self.root / ".staging"
        self._staging.mkdir(parents=True, exist_ok=True, mode=0o700)

    async def stage_upload(
        self, upload: UploadFile, *, max_bytes: int, allowed_mime: set[str]
    ) -> StagedUpload:
        filename = _safe_filename(upload.filename)
        descriptor, temp_name = tempfile.mkstemp(prefix="upload-", dir=self._staging)
        temp_path = Path(temp_name)
        digest = hashlib.sha256()
        size = 0
        prefix = bytearray()
        try:
            with os.fdopen(descriptor, "wb") as destination:
                os.chmod(temp_path, 0o600)
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise UploadValidationError("upload exceeds UPLOAD_MAX_MB")
                    digest.update(chunk)
                    if len(prefix) < 8192:
                        prefix.extend(chunk[: 8192 - len(prefix)])
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            if size == 0:
                raise UploadValidationError("empty files are not accepted")
            try:
                mime = sniff_mime(filename, upload.content_type, bytes(prefix))
            except ParseError as exc:
                raise UploadValidationError(str(exc)) from exc
            if mime not in allowed_mime:
                raise UploadValidationError("MIME type is not allowed")
            return StagedUpload(
                temp_path=temp_path,
                source_filename=filename,
                mime=mime,
                file_sha256=digest.hexdigest(),
                size_bytes=size,
            )
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()

    def commit(
        self,
        staged: StagedUpload,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        document_version_id: uuid.UUID,
    ) -> str:
        suffix = Path(staged.source_filename).suffix.casefold()
        relative = (
            Path(str(tenant_id)) / str(document_id) / str(document_version_id) / f"original{suffix}"
        )
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=False, mode=0o700)
        os.replace(staged.temp_path, destination)
        return relative.as_posix()

    def resolve(self, storage_key: str) -> Path:
        candidate = (self.root / storage_key).resolve()
        if candidate == self.root or self.root not in candidate.parents:
            raise UploadValidationError("unsafe storage key")
        return candidate

    def discard(self, staged: StagedUpload) -> None:
        staged.temp_path.unlink(missing_ok=True)

    def delete(self, storage_key: str) -> None:
        path = self.resolve(storage_key)
        path.unlink(missing_ok=True)
        for parent in (path.parent, path.parent.parent, path.parent.parent.parent):
            try:
                parent.rmdir()
            except OSError:
                break

    def clone(
        self,
        storage_key: str,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        document_version_id: uuid.UUID,
    ) -> str:
        source = self.resolve(storage_key)
        relative = Path(str(tenant_id)) / str(document_id) / str(document_version_id) / source.name
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=False, mode=0o700)
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o600)
        return relative.as_posix()


def _safe_filename(value: str | None) -> str:
    if value is None:
        raise UploadValidationError("filename is required")
    candidate = value.strip()
    if (
        not candidate
        or PurePath(candidate).name != candidate
        or SAFE_FILENAME_RE.fullmatch(candidate) is None
    ):
        raise UploadValidationError("unsafe filename")
    return candidate
