from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePath
from typing import Annotated, Literal

import anyio
import orjson
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal, get_current_principal
from app.auth.rate_limit import RateLimiter
from app.core.db import get_db_session
from app.ingest.storage import LocalDocumentStorage, UploadValidationError
from app.models import Document, DocumentVersion, User, UserTenant

router = APIRouter()
SAFE_DOWNLOAD_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,255}$")


class SignedUrlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_version_id: uuid.UUID | None = None
    page: int | None = Field(default=None, ge=1)


class SignedUrlResponse(BaseModel):
    url: str
    expires_at: datetime
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    page: int | None


class FileTokenPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    storage_binding: str = Field(pattern=r"^[0-9a-f]{64}$")
    page: int | None = Field(default=None, ge=1)
    expires_at: int = Field(gt=0)
    nonce: str = Field(min_length=16, max_length=64)


class FileTokenError(ValueError):
    def __init__(self, code: Literal["invalid", "expired"]) -> None:
        super().__init__(code)
        self.code = code


class FileTokenSigner:
    def __init__(self, secret: str, *, ttl_seconds: int) -> None:
        self._secret = secret.encode()
        self._ttl = ttl_seconds

    def issue(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
        version: DocumentVersion,
        page: int | None,
        now: datetime | None = None,
    ) -> tuple[str, datetime]:
        issued_at = now or datetime.now(UTC)
        expires_at = issued_at + timedelta(seconds=self._ttl)
        payload = FileTokenPayload(
            tenant_id=tenant_id,
            user_id=user_id,
            document_id=document_id,
            document_version_id=version.id,
            file_sha256=version.file_sha256,
            storage_binding=self.storage_binding(
                tenant_id=tenant_id,
                document_id=document_id,
                version_id=version.id,
                storage_key=version.storage_key,
                file_sha256=version.file_sha256,
            ),
            page=page,
            expires_at=int(expires_at.timestamp()),
            nonce=secrets.token_urlsafe(18),
        )
        encoded = _b64encode(orjson.dumps(payload.model_dump(mode="json")))
        signature = self._signature(encoded)
        return f"{encoded}.{signature}", expires_at

    def verify(
        self,
        token: str,
        *,
        now: datetime | None = None,
    ) -> FileTokenPayload:
        if len(token) > 4_096:
            raise FileTokenError("invalid")
        try:
            encoded, provided = token.split(".", maxsplit=1)
        except ValueError as exc:
            raise FileTokenError("invalid") from exc
        expected = self._signature(encoded)
        if not hmac.compare_digest(provided, expected):
            raise FileTokenError("invalid")
        try:
            payload = FileTokenPayload.model_validate(orjson.loads(_b64decode(encoded)))
        except (ValueError, ValidationError, orjson.JSONDecodeError) as exc:
            raise FileTokenError("invalid") from exc
        current = int((now or datetime.now(UTC)).timestamp())
        if current >= payload.expires_at:
            raise FileTokenError("expired")
        return payload

    def storage_binding(
        self,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        version_id: uuid.UUID,
        storage_key: str,
        file_sha256: str,
    ) -> str:
        value = "\0".join(
            (str(tenant_id), str(document_id), str(version_id), storage_key, file_sha256)
        ).encode()
        return hmac.new(
            self._secret,
            b"rag-file-storage-v1\0" + value,
            hashlib.sha256,
        ).hexdigest()

    def _signature(self, encoded: str) -> str:
        digest = hmac.new(
            self._secret,
            b"rag-file-token-v1\0" + encoded.encode(),
            hashlib.sha256,
        ).digest()
        return _b64encode(digest)


@router.post(
    "/api/documents/{document_id}/signed-url",
    response_model=SignedUrlResponse,
)
async def create_signed_url(
    document_id: uuid.UUID,
    body: SignedUrlRequest,
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> SignedUrlResponse:
    limiter: RateLimiter = request.app.state.rate_limiter
    decision = await limiter.check(
        "signed_file",
        {"tenant": str(principal.tenant_id), "user": str(principal.user_id)},
        request.app.state.settings.signed_url_rate_limits,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Signed file rate limit exceeded",
            headers={"Retry-After": str(decision.retry_after)},
        )
    document = await session.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.tenant_id == principal.tenant_id,
            Document.deleted_at.is_(None),
        )
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    version_id = body.document_version_id or document.active_version_id
    if version_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No readable version")
    version = await session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.id == version_id,
            DocumentVersion.tenant_id == principal.tenant_id,
            DocumentVersion.document_id == document.id,
            DocumentVersion.status.in_(("active", "superseded", "ready")),
        )
    )
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if body.page is not None and (version.page_count < body.page or version.page_count == 0):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Page is outside the document",
        )
    signer: FileTokenSigner = request.app.state.file_token_signer
    token, expires_at = signer.issue(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        document_id=document.id,
        version=version,
        page=body.page,
    )
    fragment = f"#page={body.page}" if body.page is not None else ""
    return SignedUrlResponse(
        url=f"/api/files/{token}{fragment}",
        expires_at=expires_at,
        document_id=document.id,
        document_version_id=version.id,
        page=body.page,
    )


@router.get("/api/files/{token}", response_class=FileResponse)
async def read_signed_file(
    token: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> FileResponse:
    signer: FileTokenSigner = request.app.state.file_token_signer
    try:
        payload = signer.verify(token)
    except FileTokenError as exc:
        if exc.code == "expired":
            raise HTTPException(
                status_code=status.HTTP_410_GONE, detail="Signed link expired"
            ) from exc
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found") from exc
    row = (
        await session.execute(
            select(Document, DocumentVersion, User)
            .join(
                DocumentVersion,
                (DocumentVersion.document_id == Document.id)
                & (DocumentVersion.tenant_id == Document.tenant_id),
            )
            .join(
                UserTenant,
                (UserTenant.user_id == payload.user_id)
                & (UserTenant.tenant_id == payload.tenant_id),
            )
            .join(User, User.id == UserTenant.user_id)
            .where(
                Document.id == payload.document_id,
                Document.tenant_id == payload.tenant_id,
                Document.deleted_at.is_(None),
                DocumentVersion.id == payload.document_version_id,
                DocumentVersion.tenant_id == payload.tenant_id,
                DocumentVersion.status.in_(("active", "superseded", "ready")),
                User.id == payload.user_id,
                User.is_active.is_(True),
                User.disabled_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    document, version, _user = row
    binding = signer.storage_binding(
        tenant_id=version.tenant_id,
        document_id=version.document_id,
        version_id=version.id,
        storage_key=version.storage_key,
        file_sha256=version.file_sha256,
    )
    if (
        version.file_sha256 != payload.file_sha256
        or not hmac.compare_digest(binding, payload.storage_binding)
        or (
            payload.page is not None
            and (version.page_count == 0 or payload.page > version.page_count)
        )
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    storage: LocalDocumentStorage = request.app.state.document_storage
    try:
        path = storage.resolve(version.storage_key)
    except UploadValidationError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found") from exc
    actual_size, actual_hash = await anyio.to_thread.run_sync(_file_identity, path)
    if actual_size != version.file_size_bytes or not hmac.compare_digest(
        actual_hash, version.file_sha256
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return FileResponse(
        path,
        media_type=document.mime,
        filename=_safe_download_name(document.source_filename),
        content_disposition_type="inline",
        headers={
            "Cache-Control": "private, no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                size += len(block)
                digest.update(block)
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found") from exc
    return size, digest.hexdigest()


def _safe_download_name(value: str) -> str:
    candidate = PurePath(value).name.strip()
    if not candidate or SAFE_DOWNLOAD_RE.fullmatch(candidate) is None:
        return "document"
    return candidate


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
