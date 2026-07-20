from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
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
