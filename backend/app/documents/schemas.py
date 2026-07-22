from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

VersionStatus = Literal["staged", "processing", "ready", "active", "failed", "superseded"]


class UploadAccepted(BaseModel):
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    version: int
    job_id: uuid.UUID
    status: VersionStatus


class DocumentVersionResponse(BaseModel):
    id: uuid.UUID
    version: int
    status: VersionStatus
    file_sha256: str
    file_size_bytes: int
    page_count: int
    section_count: int
    chunk_count: int
    error_code: str | None
    error_detail: str | None
    created_at: datetime
    activated_at: datetime | None


class DocumentResponse(BaseModel):
    id: uuid.UUID
    title: str
    source_filename: str
    mime: str
    active_version_id: uuid.UUID | None
    collection_ids: list[uuid.UUID]
    created_at: datetime
    versions: list[DocumentVersionResponse]


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]
    active_index_generation_id: int | None
    retrieval_revision: int


class DeleteDocumentResponse(BaseModel):
    retrieval_revision: int
