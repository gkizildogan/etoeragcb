from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import stat
import tarfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.db import create_database_engine, create_session_factory
from app.documents.files import FileTokenError, FileTokenSigner
from app.models import (
    ChatSession,
    Chunk,
    Document,
    DocumentVersion,
    Feedback,
    IdempotencyRequest,
    IndexGeneration,
    IndexGenerationDocument,
    IngestionJob,
    KnowledgeCollection,
    Message,
    RefreshToken,
    Section,
    Tenant,
    User,
    UserTenant,
)

SCHEMA_VERSION = 1
TABLE_MODELS = {
    "tenants": Tenant,
    "users": User,
    "user_tenants": UserTenant,
    "refresh_tokens": RefreshToken,
    "chat_sessions": ChatSession,
    "collections": KnowledgeCollection,
    "documents": Document,
    "document_versions": DocumentVersion,
    "sections": Section,
    "chunks": Chunk,
    "index_generations": IndexGeneration,
    "index_generation_documents": IndexGenerationDocument,
    "ingestion_jobs": IngestionJob,
    "messages": Message,
    "idempotency_requests": IdempotencyRequest,
    "feedback": Feedback,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare and verify encrypted backup data")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--stage", type=Path, required=True)

    live = subparsers.add_parser("verify-live")
    live.add_argument("--report", type=Path)
    live.add_argument("--allow-empty", action="store_true")

    restore = subparsers.add_parser("restore-assets")
    restore.add_argument("--stage", type=Path, required=True)
    restore.add_argument("--documents-root", type=Path, required=True)
    restore.add_argument("--qdrant-url", default="http://qdrant:6333")
    restore.add_argument("--collection", required=True)

    verify = subparsers.add_parser("verify-restore")
    verify.add_argument("--stage", type=Path, required=True)
    verify.add_argument("--documents-root", type=Path, required=True)
    verify.add_argument("--database-url-file", type=Path, required=True)
    verify.add_argument("--signing-secret-file", type=Path, required=True)
    verify.add_argument("--qdrant-url", default="http://qdrant:6333")
    verify.add_argument("--collection", required=True)
    verify.add_argument("--report", type=Path)
    verify.add_argument("--allow-empty", action="store_true")
    verify.add_argument(
        "--backup-source",
        choices=("local", "off-machine"),
        default="local",
    )
    return parser


async def prepare_backup(stage: Path) -> dict[str, Any]:
    settings = get_settings()
    stage = stage.resolve()
    postgres_dump = stage / "postgres.dump"
    if not postgres_dump.is_file() or postgres_dump.stat().st_size == 0:
        raise RuntimeError("postgres.dump must exist before preparing backup assets")

    engine = create_database_engine(settings.resolved_database_url())
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await _read_only_repeatable_read(session)
            counts = await _table_counts(session)
            generations = await _active_generations(session)
            files = await _file_manifest(session, settings.document_storage_root)
            await session.rollback()
    finally:
        await engine.dispose()

    documents_archive = stage / "documents.tar"
    await asyncio.to_thread(_archive_documents, settings.document_storage_root, documents_archive)
    qdrant_snapshot = stage / "qdrant.snapshot"
    snapshot_name = await _download_qdrant_snapshot(
        str(settings.qdrant_url),
        settings.qdrant_collection,
        qdrant_snapshot,
    )
    artifacts = {
        item.name: {
            "bytes": item.stat().st_size,
            "sha256": await asyncio.to_thread(_sha256_file, item),
        }
        for item in (postgres_dump, documents_archive, qdrant_snapshot)
    }
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "prepared_at": datetime.now(UTC).isoformat(),
        "qdrant_collection": settings.qdrant_collection,
        "qdrant_snapshot_name": snapshot_name,
        "table_counts": counts,
        "active_generations": generations,
        "raw_files": files,
        "artifacts": artifacts,
    }
    _write_json_atomic(stage / "manifest.json", manifest)
    return manifest


async def verify_live(*, allow_empty: bool = False) -> dict[str, Any]:
    settings = get_settings()
    engine = create_database_engine(settings.resolved_database_url())
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            generations = await _active_generations(session)
            files = await _file_manifest(session, settings.document_storage_root)
            active_chunks = await _verify_qdrant_active_versions(
                session,
                qdrant_url=str(settings.qdrant_url),
                collection=settings.qdrant_collection,
            )
            if files and active_chunks < 1:
                raise RuntimeError("live consistency check requires an active indexed chunk")
            if files:
                signed_link = await _verify_signed_link(
                    session,
                    signing_secret=settings.resolved_signing_secret().get_secret_value(),
                    documents_root=settings.document_storage_root,
                )
            elif allow_empty:
                signed_link = {"result": "not_applicable_no_active_document"}
            else:
                raise RuntimeError("live consistency check requires an active document")
    finally:
        await engine.dispose()
    eligible = bool(files and active_chunks)
    return {
        "schema_version": SCHEMA_VERSION,
        "verified_at": datetime.now(UTC).isoformat(),
        "active_generations": generations,
        "raw_files": len(files),
        "retrievable_active_chunks": active_chunks,
        "signed_link": signed_link,
        "release_gate_eligible": eligible,
        "result": "pass" if eligible else "pass_with_empty_data",
    }


async def restore_assets(
    stage: Path,
    documents_root: Path,
    *,
    qdrant_url: str,
    collection: str,
) -> None:
    manifest = _load_manifest(stage)
    _verify_artifacts(stage, manifest)
    documents_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    await asyncio.to_thread(
        _extract_documents,
        stage / "documents.tar",
        documents_root,
    )
    await _upload_qdrant_snapshot(
        qdrant_url,
        collection,
        stage / "qdrant.snapshot",
    )


async def verify_restore(
    stage: Path,
    documents_root: Path,
    *,
    database_url_file: Path,
    signing_secret_file: Path,
    qdrant_url: str,
    collection: str,
    allow_empty: bool = False,
    backup_source: str = "local",
) -> dict[str, Any]:
    manifest = _load_manifest(stage)
    _verify_artifacts(stage, manifest)
    database_url = database_url_file.read_text(encoding="utf-8").strip()
    signing_secret = signing_secret_file.read_text(encoding="utf-8").strip()
    engine = create_database_engine(database_url)
    factory = create_session_factory(engine)
    checks: dict[str, Any] = {}
    try:
        async with factory() as session:
            restored_counts = await _table_counts(session)
            if restored_counts != manifest["table_counts"]:
                raise RuntimeError("restored PostgreSQL table counts differ from backup manifest")
            checks["postgres_counts"] = restored_counts

            generations = await _active_generations(session)
            if generations != manifest["active_generations"]:
                raise RuntimeError("restored active generations differ from backup manifest")
            checks["active_generations"] = generations

            file_rows = list(
                (
                    await session.execute(
                        select(Document, DocumentVersion)
                        .join(
                            DocumentVersion,
                            (DocumentVersion.id == Document.active_version_id)
                            & (DocumentVersion.tenant_id == Document.tenant_id),
                        )
                        .where(Document.deleted_at.is_(None))
                    )
                ).all()
            )
            if not file_rows:
                if not allow_empty:
                    raise RuntimeError("restore drill requires at least one active document")
            for _document, version in file_rows:
                path = _safe_storage_path(documents_root, version.storage_key)
                size, digest = await asyncio.to_thread(_file_identity, path)
                if size != version.file_size_bytes or digest != version.file_sha256:
                    raise RuntimeError(f"restored raw file identity mismatch for {version.id}")
            checks["active_files"] = len(file_rows)

            indexed_chunks = await _verify_qdrant_active_versions(
                session,
                qdrant_url=qdrant_url,
                collection=collection,
            )
            if file_rows and indexed_chunks < 1:
                raise RuntimeError("restore drill requires at least one retrievable active chunk")
            checks["retrievable_active_chunks"] = indexed_chunks

            if file_rows:
                checks["signed_link"] = await _verify_signed_link(
                    session,
                    signing_secret=signing_secret,
                    documents_root=documents_root,
                )
            else:
                checks["signed_link"] = {"result": "not_applicable_no_active_document"}
    finally:
        await engine.dispose()
    checks["verified_at"] = datetime.now(UTC).isoformat()
    checks["backup_source"] = backup_source
    checks["release_gate_eligible"] = bool(file_rows and indexed_chunks)
    checks["result"] = "pass" if checks["release_gate_eligible"] else "pass_with_empty_data"
    return checks


async def _read_only_repeatable_read(session: AsyncSession) -> None:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))


async def _table_counts(session: AsyncSession) -> dict[str, int]:
    return {
        name: int(await session.scalar(select(func.count()).select_from(model)) or 0)
        for name, model in TABLE_MODELS.items()
    }


async def _active_generations(session: AsyncSession) -> list[dict[str, Any]]:
    tenants = list(await session.scalars(select(Tenant).order_by(Tenant.id)))
    result: list[dict[str, Any]] = []
    for tenant in tenants:
        version_ids: list[str] = []
        if tenant.active_index_generation_id is not None:
            generation = await session.scalar(
                select(IndexGeneration).where(
                    IndexGeneration.id == tenant.active_index_generation_id,
                    IndexGeneration.tenant_id == tenant.id,
                    IndexGeneration.status == "active",
                )
            )
            if generation is None:
                raise RuntimeError(f"tenant {tenant.id} has an invalid active generation")
            version_ids = [
                str(item)
                for item in await session.scalars(
                    select(IndexGenerationDocument.document_version_id)
                    .where(
                        IndexGenerationDocument.tenant_id == tenant.id,
                        IndexGenerationDocument.generation_id == tenant.active_index_generation_id,
                    )
                    .order_by(IndexGenerationDocument.document_id)
                )
            ]
        result.append(
            {
                "tenant_id": str(tenant.id),
                "retrieval_revision": tenant.retrieval_revision,
                "active_generation_id": tenant.active_index_generation_id,
                "document_version_ids": version_ids,
            }
        )
    return result


async def _file_manifest(session: AsyncSession, root: Path) -> list[dict[str, Any]]:
    versions = list(
        await session.scalars(
            select(DocumentVersion)
            .where(
                DocumentVersion.storage_key != "pending",
                DocumentVersion.garbage_collected_at.is_(None),
            )
            .order_by(
                DocumentVersion.tenant_id, DocumentVersion.document_id, DocumentVersion.version
            )
        )
    )
    result: list[dict[str, Any]] = []
    for version in versions:
        path = _safe_storage_path(root, version.storage_key)
        if not path.is_file():
            raise RuntimeError(f"raw file is missing for document version {version.id}")
        size, digest = await asyncio.to_thread(_file_identity, path)
        if size != version.file_size_bytes or digest != version.file_sha256:
            raise RuntimeError(f"raw file identity mismatch for document version {version.id}")
        result.append(
            {
                "tenant_id": str(version.tenant_id),
                "document_id": str(version.document_id),
                "document_version_id": str(version.id),
                "storage_key": version.storage_key,
                "bytes": size,
                "sha256": digest,
            }
        )
    return result


def _archive_documents(root: Path, destination: Path) -> None:
    root = root.resolve()
    with tarfile.open(destination, "w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if ".staging" in relative.parts:
                continue
            if path.is_symlink():
                raise RuntimeError(f"document storage contains a symlink: {relative}")
            if path.is_file():
                archive.add(path, arcname=(Path("documents") / relative).as_posix())
    os.chmod(destination, 0o640)


def _extract_documents(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path, "r") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if (
                not member.isfile()
                or path.is_absolute()
                or not path.parts
                or path.parts[0] != "documents"
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise RuntimeError("documents archive contains an unsafe entry")
            relative = Path(*path.parts[1:])
            target = _safe_storage_path(destination, relative.as_posix())
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError("documents archive entry cannot be read")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            os.chmod(target, 0o600)


async def _download_qdrant_snapshot(url: str, collection: str, destination: Path) -> str:
    collection_path = quote(collection, safe="")
    timeout = httpx.Timeout(300.0, connect=10.0)
    async with httpx.AsyncClient(base_url=url.rstrip("/"), timeout=timeout) as client:
        response = await client.post(
            f"/collections/{collection_path}/snapshots", params={"wait": "true"}
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result") if isinstance(payload, dict) else None
        snapshot_name = result.get("name") if isinstance(result, dict) else None
        if not isinstance(snapshot_name, str) or not snapshot_name:
            raise RuntimeError("Qdrant did not return a snapshot name")
        snapshot_path = quote(snapshot_name, safe="")
        try:
            async with client.stream(
                "GET",
                f"/collections/{collection_path}/snapshots/{snapshot_path}",
            ) as download:
                download.raise_for_status()
                with destination.open("xb") as output:
                    async for block in download.aiter_bytes():
                        output.write(block)
                os.chmod(destination, 0o640)
        finally:
            cleanup = await client.delete(
                f"/collections/{collection_path}/snapshots/{snapshot_path}"
            )
            cleanup.raise_for_status()
    return snapshot_name


async def _upload_qdrant_snapshot(
    url: str,
    collection: str,
    snapshot_path: Path,
) -> None:
    collection_path = quote(collection, safe="")
    timeout = httpx.Timeout(600.0, connect=10.0)
    async with httpx.AsyncClient(base_url=url.rstrip("/"), timeout=timeout) as client:
        with snapshot_path.open("rb") as source:
            response = await client.post(
                f"/collections/{collection_path}/snapshots/upload",
                params={"wait": "true", "priority": "snapshot"},
                files={"snapshot": (snapshot_path.name, source, "application/octet-stream")},
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise RuntimeError("Qdrant snapshot upload did not complete successfully")


async def _verify_qdrant_active_versions(
    session: AsyncSession,
    *,
    qdrant_url: str,
    collection: str,
) -> int:
    versions = list(
        (
            await session.execute(
                select(
                    IndexGenerationDocument.tenant_id,
                    IndexGenerationDocument.document_version_id,
                )
                .join(
                    Tenant,
                    (Tenant.id == IndexGenerationDocument.tenant_id)
                    & (Tenant.active_index_generation_id == IndexGenerationDocument.generation_id),
                )
                .order_by(
                    IndexGenerationDocument.tenant_id,
                    IndexGenerationDocument.document_version_id,
                )
            )
        ).all()
    )
    collection_path = quote(collection, safe="")
    total = 0
    async with httpx.AsyncClient(base_url=qdrant_url.rstrip("/"), timeout=30.0) as client:
        for tenant_id, version_id in versions:
            expected = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Chunk)
                    .where(
                        Chunk.tenant_id == tenant_id,
                        Chunk.document_version_id == version_id,
                    )
                )
                or 0
            )
            response = await client.post(
                f"/collections/{collection_path}/points/count",
                json={
                    "exact": True,
                    "filter": {
                        "must": [
                            {
                                "key": "tenant_id",
                                "match": {"value": str(tenant_id)},
                            },
                            {
                                "key": "document_version_id",
                                "match": {"value": str(version_id)},
                            },
                        ]
                    },
                },
            )
            response.raise_for_status()
            payload = response.json()
            result = payload.get("result") if isinstance(payload, dict) else None
            actual = result.get("count") if isinstance(result, dict) else None
            if actual != expected:
                raise RuntimeError(
                    f"Qdrant count mismatch for active version {version_id}: "
                    f"expected {expected}, found {actual}"
                )
            total += expected
    return total


async def _verify_signed_link(
    session: AsyncSession,
    *,
    signing_secret: str,
    documents_root: Path,
) -> dict[str, Any]:
    row = (
        await session.execute(
            select(User, UserTenant, Document, DocumentVersion)
            .join(UserTenant, UserTenant.user_id == User.id)
            .join(
                Document,
                (Document.tenant_id == UserTenant.tenant_id) & (Document.deleted_at.is_(None)),
            )
            .join(
                DocumentVersion,
                (DocumentVersion.id == Document.active_version_id)
                & (DocumentVersion.tenant_id == Document.tenant_id),
            )
            .where(User.is_active.is_(True), User.disabled_at.is_(None))
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        raise RuntimeError("restore drill needs an active user and document for signed-link proof")
    user, membership, document, version = row
    signer = FileTokenSigner(signing_secret, ttl_seconds=60)
    now = datetime.now(UTC)
    token, expires_at = signer.issue(
        tenant_id=membership.tenant_id,
        user_id=user.id,
        document_id=document.id,
        version=version,
        page=1 if version.page_count else None,
        now=now,
    )
    payload = signer.verify(token, now=now)
    try:
        signer.verify(token, now=expires_at + timedelta(seconds=1))
    except FileTokenError as exc:
        if exc.code != "expired":
            raise RuntimeError("restored signed link failed with the wrong reason") from exc
    else:
        raise RuntimeError("restored signed link did not expire")
    path = _safe_storage_path(documents_root, version.storage_key)
    if not path.is_file() or payload.document_version_id != version.id:
        raise RuntimeError("restored signed link does not bind to a restored file")
    return {
        "issued": True,
        "verified": True,
        "expiry_rejected": True,
        "document_version_id": str(version.id),
    }


def _load_manifest(stage: Path) -> dict[str, Any]:
    try:
        payload = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("backup manifest cannot be read") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("unsupported backup manifest")
    if payload.get("qdrant_collection") is None or not isinstance(payload.get("artifacts"), dict):
        raise RuntimeError("backup manifest is incomplete")
    return payload


def _verify_artifacts(stage: Path, manifest: dict[str, Any]) -> None:
    artifacts = manifest["artifacts"]
    for name in ("postgres.dump", "documents.tar", "qdrant.snapshot"):
        expected = artifacts.get(name)
        path = stage / name
        if (
            not isinstance(expected, dict)
            or expected.get("bytes") != path.stat().st_size
            or expected.get("sha256") != _sha256_file(path)
        ):
            raise RuntimeError(f"backup artifact verification failed: {name}")


def _safe_storage_path(root: Path, storage_key: str) -> Path:
    resolved_root = root.resolve()
    candidate = (resolved_root / storage_key).resolve()
    if candidate == resolved_root or resolved_root not in candidate.parents:
        raise RuntimeError("unsafe document storage key")
    return candidate


def _file_identity(path: Path) -> tuple[int, str]:
    return path.stat().st_size, _sha256_file(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(f"{path.suffix}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(
        temporary,
        flags,
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def run(args: argparse.Namespace) -> None:
    if args.command == "prepare":
        await prepare_backup(args.stage)
    elif args.command == "verify-live":
        report = await verify_live(allow_empty=args.allow_empty)
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(args.report, report)
        print(json.dumps(report, indent=2, sort_keys=True))
    elif args.command == "restore-assets":
        await restore_assets(
            args.stage,
            args.documents_root,
            qdrant_url=args.qdrant_url,
            collection=args.collection,
        )
    elif args.command == "verify-restore":
        report = await verify_restore(
            args.stage,
            args.documents_root,
            database_url_file=args.database_url_file,
            signing_secret_file=args.signing_secret_file,
            qdrant_url=args.qdrant_url,
            collection=args.collection,
            allow_empty=args.allow_empty,
            backup_source=args.backup_source,
        )
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(args.report, report)
        print(json.dumps(report, indent=2, sort_keys=True))
    else:  # pragma: no cover - argparse enforces commands
        raise RuntimeError("unsupported backup command")


def main() -> None:
    try:
        asyncio.run(run(build_parser().parse_args()))
    except (OSError, RuntimeError, httpx.HTTPError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
