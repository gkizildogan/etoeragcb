from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utc_now

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")
GENERATION_ID = BigInteger().with_variant(Integer(), "sqlite")


class KnowledgeCollection(Base):
    __tablename__ = "collections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["user_tenants.user_id", "user_tenants.tenant_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index(
    "uq_collections_active_tenant_name",
    KnowledgeCollection.tenant_id,
    func.lower(KnowledgeCollection.name),
    unique=True,
    postgresql_where=KnowledgeCollection.deleted_at.is_(None),
    sqlite_where=KnowledgeCollection.deleted_at.is_(None),
)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["user_tenants.user_id", "user_tenants.tenant_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["active_version_id", "tenant_id", "id"],
            [
                "document_versions.id",
                "document_versions.tenant_id",
                "document_versions.document_id",
            ],
            name="fk_documents_active_version",
            use_alter=True,
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "tenant_id"),
        Index("ix_documents_tenant_active", "tenant_id", "deleted_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    source_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime: Mapped[str] = mapped_column(String(200), nullable=False)
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('staged', 'processing', 'ready', 'active', 'failed', 'superseded')",
            name="valid_status",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("file_size_bytes > 0", name="file_size_positive"),
        CheckConstraint("page_count >= 0", name="page_count_nonnegative"),
        CheckConstraint("section_count >= 0", name="section_count_nonnegative"),
        CheckConstraint("chunk_count >= 0", name="chunk_count_nonnegative"),
        ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["documents.id", "documents.tenant_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["index_generation_id", "tenant_id"],
            ["index_generations.id", "index_generations.tenant_id"],
            name="fk_document_versions_generation",
            use_alter=True,
            ondelete="RESTRICT",
        ),
        UniqueConstraint("document_id", "version"),
        UniqueConstraint(
            "id", "tenant_id", "document_id", name="uq_document_versions_id_tenant_document"
        ),
        UniqueConstraint("id", "tenant_id", name="uq_document_versions_id_tenant"),
        Index("ix_document_versions_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="staged")
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    section_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    index_generation_id: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_detail: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    garbage_collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Section(Base):
    __tablename__ = "sections"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ordinal_nonnegative"),
        CheckConstraint("level >= 1", name="level_positive"),
        CheckConstraint("page_start >= 1", name="page_start_positive"),
        CheckConstraint("page_end >= page_start", name="page_order"),
        ForeignKeyConstraint(
            ["document_version_id", "tenant_id", "document_id"],
            [
                "document_versions.id",
                "document_versions.tenant_id",
                "document_versions.document_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["parent_id", "tenant_id", "document_version_id"],
            ["sections.id", "sections.tenant_id", "sections.document_version_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "tenant_id", "document_version_id"),
        UniqueConstraint("document_version_id", "ordinal"),
        Index("ix_sections_tenant_document", "tenant_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    document_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_original: Mapped[str] = mapped_column(Text, nullable=False)
    heading_lexical: Mapped[str] = mapped_column(Text, nullable=False)
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    path_original: Mapped[str] = mapped_column(Text, nullable=False)
    path_lexical: Mapped[str] = mapped_column(Text, nullable=False)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict
    )


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        CheckConstraint("occurrence_index >= 0", name="occurrence_nonnegative"),
        CheckConstraint("chunk_index >= 0", name="chunk_index_nonnegative"),
        CheckConstraint("page_start >= 1", name="page_start_positive"),
        CheckConstraint("page_end >= page_start", name="page_order"),
        CheckConstraint("char_start >= 0", name="char_start_nonnegative"),
        CheckConstraint("char_end >= char_start", name="char_order"),
        CheckConstraint("token_count >= 0", name="token_count_nonnegative"),
        ForeignKeyConstraint(
            ["document_version_id", "tenant_id", "document_id"],
            [
                "document_versions.id",
                "document_versions.tenant_id",
                "document_versions.document_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["section_id", "tenant_id", "document_version_id"],
            ["sections.id", "sections.tenant_id", "sections.document_version_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("document_version_id", "chunk_index"),
        Index("ix_chunks_tenant_document_version", "tenant_id", "document_version_id"),
        Index("ix_chunks_content_sha256", "tenant_id", "content_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    document_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    section_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    occurrence_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    lexical_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    text_original: Mapped[str] = mapped_column(Text, nullable=False)
    text_lexical: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class IndexGeneration(Base):
    __tablename__ = "index_generations"
    __table_args__ = (
        CheckConstraint("status IN ('preparing', 'active', 'failed')", name="valid_status"),
        CheckConstraint("retrieval_revision >= 1", name="revision_positive"),
        ForeignKeyConstraint(
            ["changed_document_version_id", "tenant_id"],
            ["document_versions.id", "document_versions.tenant_id"],
            name="fk_index_generations_changed_version",
            use_alter=True,
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["parent_generation_id", "tenant_id"],
            ["index_generations.id", "index_generations.tenant_id"],
            name="fk_index_generations_parent",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "tenant_id"),
        Index("ix_index_generations_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(GENERATION_ID, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    reason: Mapped[str] = mapped_column(String(80), nullable=False)
    changed_document_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    parent_generation_id: Mapped[int | None] = mapped_column(GENERATION_ID)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="preparing")
    retrieval_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IndexGenerationDocument(Base):
    __tablename__ = "index_generation_documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["generation_id", "tenant_id"],
            ["index_generations.id", "index_generations.tenant_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["document_version_id", "tenant_id", "document_id"],
            [
                "document_versions.id",
                "document_versions.tenant_id",
                "document_versions.document_id",
            ],
            ondelete="RESTRICT",
        ),
    )

    generation_id: Mapped[int] = mapped_column(GENERATION_ID, primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    document_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('staged', 'queued', 'processing', 'succeeded', 'failed')",
            name="valid_status",
        ),
        CheckConstraint("attempt >= 0", name="attempt_nonnegative"),
        ForeignKeyConstraint(
            ["document_version_id", "tenant_id"],
            ["document_versions.id", "document_versions.tenant_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("document_version_id"),
        Index("ix_ingestion_jobs_reconcile", "status", "heartbeat_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    document_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    arq_job_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="staged")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class DocumentCollection(Base):
    __tablename__ = "document_collections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["documents.id", "documents.tenant_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["collection_id", "tenant_id"],
            ["collections.id", "collections.tenant_id"],
            ondelete="CASCADE",
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    collection_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant', 'system')", name="valid_role"),
        ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["user_tenants.user_id", "user_tenants.tenant_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["session_id", "tenant_id", "user_id"],
            ["chat_sessions.id", "chat_sessions.tenant_id", "chat_sessions.user_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "tenant_id", "user_id"),
        UniqueConstraint("tenant_id", "user_id", "client_request_id"),
        Index("ix_messages_session_order", "tenant_id", "session_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    client_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class IdempotencyRequest(Base):
    __tablename__ = "idempotency_requests"
    __table_args__ = (
        CheckConstraint("status IN ('in_progress', 'completed', 'failed')", name="valid_status"),
        ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["user_tenants.user_id", "user_tenants.tenant_id"],
            ondelete="CASCADE",
        ),
        Index("ix_idempotency_requests_expiry", "expires_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    operation: Mapped[str] = mapped_column(String(80), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        CheckConstraint("rating IN (-1, 1)", name="valid_rating"),
        ForeignKeyConstraint(
            ["message_id", "tenant_id", "user_id"],
            ["messages.id", "messages.tenant_id", "messages.user_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("message_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
