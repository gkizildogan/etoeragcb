"""Add sessions, messages, feedback, collections, and idempotency.

Revision ID: 0003_p3_collaboration
Revises: 0002_p2_auth_tenancy
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_p3_collaboration"
down_revision: str | None = "0002_p2_auth_tenancy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("retrieval_revision", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_unique_constraint(
        op.f("uq_chat_sessions_id"),
        "chat_sessions",
        ["id", "tenant_id", "user_id"],
    )
    op.create_table(
        "collections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["user_tenants.user_id", "user_tenants.tenant_id"],
            name=op.f("fk_collections_created_by_user_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collections")),
        sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_collections_id")),
    )
    op.create_index(
        "uq_collections_active_tenant_name",
        "collections",
        ["tenant_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("source_filename", sa.String(length=512), nullable=False),
        sa.Column("mime", sa.String(length=200), nullable=False),
        sa.Column("active_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["user_tenants.user_id", "user_tenants.tenant_id"],
            name=op.f("fk_documents_created_by_user_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_documents_id")),
    )
    op.create_index(
        "ix_documents_tenant_active",
        "documents",
        ["tenant_id", "deleted_at"],
        unique=False,
    )
    op.create_table(
        "document_collections",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("collection_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["collection_id", "tenant_id"],
            ["collections.id", "collections.tenant_id"],
            name=op.f("fk_document_collections_collection_id_collections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["documents.id", "documents.tenant_id"],
            name=op.f("fk_document_collections_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "document_id", "collection_id", name=op.f("pk_document_collections")
        ),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "meta", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("client_request_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system')", name=op.f("ck_messages_valid_role")
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "tenant_id", "user_id"],
            ["chat_sessions.id", "chat_sessions.tenant_id", "chat_sessions.user_id"],
            name=op.f("fk_messages_session_id_chat_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["user_tenants.user_id", "user_tenants.tenant_id"],
            name=op.f("fk_messages_user_id_user_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("id", "tenant_id", "user_id", name=op.f("uq_messages_id")),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "client_request_id",
            name=op.f("uq_messages_tenant_id"),
        ),
    )
    op.create_index(
        "ix_messages_session_order",
        "messages",
        ["tenant_id", "session_id", "created_at", "id"],
        unique=False,
    )
    op.create_table(
        "idempotency_requests",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=80), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("resource_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed', 'failed')",
            name=op.f("ck_idempotency_requests_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["user_tenants.user_id", "user_tenants.tenant_id"],
            name=op.f("fk_idempotency_requests_user_id_user_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "user_id",
            "operation",
            "key",
            name=op.f("pk_idempotency_requests"),
        ),
    )
    op.create_index(
        "ix_idempotency_requests_expiry",
        "idempotency_requests",
        ["expires_at"],
        unique=False,
    )
    op.create_table(
        "feedback",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("rating IN (-1, 1)", name=op.f("ck_feedback_valid_rating")),
        sa.ForeignKeyConstraint(
            ["message_id", "tenant_id", "user_id"],
            ["messages.id", "messages.tenant_id", "messages.user_id"],
            name=op.f("fk_feedback_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
        sa.UniqueConstraint("message_id", "user_id", name=op.f("uq_feedback_message_id")),
    )


def downgrade() -> None:
    op.drop_table("feedback")
    op.drop_index("ix_idempotency_requests_expiry", table_name="idempotency_requests")
    op.drop_table("idempotency_requests")
    op.drop_index("ix_messages_session_order", table_name="messages")
    op.drop_table("messages")
    op.drop_table("document_collections")
    op.drop_index("ix_documents_tenant_active", table_name="documents")
    op.drop_table("documents")
    op.drop_index("uq_collections_active_tenant_name", table_name="collections")
    op.drop_table("collections")
    op.drop_constraint(op.f("uq_chat_sessions_id"), "chat_sessions", type_="unique")
    op.drop_column("tenants", "retrieval_revision")
