"""Add staged document ingestion and durable index generations.

Revision ID: 0004_p4_staged_ingestion
Revises: 0003_p3_collaboration
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_p4_staged_ingestion"
down_revision: str | None = "0003_p3_collaboration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("active_index_generation_id", sa.BigInteger()))

    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="staged", nullable=False),
        sa.Column("page_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("section_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("index_generation_id", sa.BigInteger()),
        sa.Column("error_code", sa.String(length=80)),
        sa.Column("error_detail", sa.String(length=1000)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('staged', 'processing', 'ready', 'active', 'failed', 'superseded')",
            name=op.f("ck_document_versions_valid_status"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_document_versions_version_positive")),
        sa.CheckConstraint(
            "file_size_bytes > 0", name=op.f("ck_document_versions_file_size_positive")
        ),
        sa.CheckConstraint(
            "page_count >= 0", name=op.f("ck_document_versions_page_count_nonnegative")
        ),
        sa.CheckConstraint(
            "section_count >= 0", name=op.f("ck_document_versions_section_count_nonnegative")
        ),
        sa.CheckConstraint(
            "chunk_count >= 0", name=op.f("ck_document_versions_chunk_count_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["documents.id", "documents.tenant_id"],
            name=op.f("fk_document_versions_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_versions")),
        sa.UniqueConstraint(
            "document_id", "version", name=op.f("uq_document_versions_document_id")
        ),
        sa.UniqueConstraint(
            "id", "tenant_id", "document_id", name="uq_document_versions_id_tenant_document"
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_document_versions_id_tenant"),
        sa.UniqueConstraint("storage_key", name=op.f("uq_document_versions_storage_key")),
    )
    op.create_index(
        "ix_document_versions_tenant_status",
        "document_versions",
        ["tenant_id", "status"],
    )

    op.create_table(
        "sections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid()),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("heading_original", sa.Text(), nullable=False),
        sa.Column("heading_lexical", sa.Text(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("path_original", sa.Text(), nullable=False),
        sa.Column("path_lexical", sa.Text(), nullable=False),
        sa.Column(
            "source_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.CheckConstraint("ordinal >= 0", name=op.f("ck_sections_ordinal_nonnegative")),
        sa.CheckConstraint("level >= 1", name=op.f("ck_sections_level_positive")),
        sa.CheckConstraint("page_start >= 1", name=op.f("ck_sections_page_start_positive")),
        sa.CheckConstraint("page_end >= page_start", name=op.f("ck_sections_page_order")),
        sa.ForeignKeyConstraint(
            ["document_version_id", "tenant_id", "document_id"],
            [
                "document_versions.id",
                "document_versions.tenant_id",
                "document_versions.document_id",
            ],
            name=op.f("fk_sections_document_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id", "tenant_id", "document_version_id"],
            ["sections.id", "sections.tenant_id", "sections.document_version_id"],
            name=op.f("fk_sections_parent_id_sections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sections")),
        sa.UniqueConstraint("id", "tenant_id", "document_version_id", name=op.f("uq_sections_id")),
        sa.UniqueConstraint(
            "document_version_id", "ordinal", name=op.f("uq_sections_document_version_id")
        ),
    )
    op.create_index("ix_sections_tenant_document", "sections", ["tenant_id", "document_id"])

    op.create_table(
        "chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid()),
        sa.Column("occurrence_index", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("lexical_sha256", sa.String(length=64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("text_original", sa.Text(), nullable=False),
        sa.Column("text_lexical", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("occurrence_index >= 0", name=op.f("ck_chunks_occurrence_nonnegative")),
        sa.CheckConstraint("chunk_index >= 0", name=op.f("ck_chunks_chunk_index_nonnegative")),
        sa.CheckConstraint("page_start >= 1", name=op.f("ck_chunks_page_start_positive")),
        sa.CheckConstraint("page_end >= page_start", name=op.f("ck_chunks_page_order")),
        sa.CheckConstraint("char_start >= 0", name=op.f("ck_chunks_char_start_nonnegative")),
        sa.CheckConstraint("char_end >= char_start", name=op.f("ck_chunks_char_order")),
        sa.CheckConstraint("token_count >= 0", name=op.f("ck_chunks_token_count_nonnegative")),
        sa.ForeignKeyConstraint(
            ["document_version_id", "tenant_id", "document_id"],
            [
                "document_versions.id",
                "document_versions.tenant_id",
                "document_versions.document_id",
            ],
            name=op.f("fk_chunks_document_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["section_id", "tenant_id", "document_version_id"],
            ["sections.id", "sections.tenant_id", "sections.document_version_id"],
            name=op.f("fk_chunks_section_id_sections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunks")),
        sa.UniqueConstraint(
            "document_version_id", "chunk_index", name=op.f("uq_chunks_document_version_id")
        ),
    )
    op.create_index(
        "ix_chunks_tenant_document_version",
        "chunks",
        ["tenant_id", "document_version_id"],
    )
    op.create_index("ix_chunks_content_sha256", "chunks", ["tenant_id", "content_sha256"])

    op.create_table(
        "index_generations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(length=80), nullable=False),
        sa.Column("changed_document_version_id", sa.Uuid()),
        sa.Column("parent_generation_id", sa.BigInteger()),
        sa.Column("status", sa.String(length=16), server_default="preparing", nullable=False),
        sa.Column("retrieval_revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('preparing', 'active', 'failed')",
            name=op.f("ck_index_generations_valid_status"),
        ),
        sa.CheckConstraint(
            "retrieval_revision >= 1", name=op.f("ck_index_generations_revision_positive")
        ),
        sa.ForeignKeyConstraint(
            ["changed_document_version_id", "tenant_id"],
            ["document_versions.id", "document_versions.tenant_id"],
            name="fk_index_generations_changed_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_generation_id", "tenant_id"],
            ["index_generations.id", "index_generations.tenant_id"],
            name="fk_index_generations_parent",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_index_generations")),
        sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_index_generations_id")),
    )
    op.create_index(
        "ix_index_generations_tenant_created",
        "index_generations",
        ["tenant_id", "created_at"],
    )

    op.create_foreign_key(
        "fk_document_versions_generation",
        "document_versions",
        "index_generations",
        ["index_generation_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_documents_active_version",
        "documents",
        "document_versions",
        ["active_version_id", "tenant_id", "id"],
        ["id", "tenant_id", "document_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_tenants_active_generation",
        "tenants",
        "index_generations",
        ["active_index_generation_id", "id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "index_generation_documents",
        sa.Column("generation_id", sa.BigInteger(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["generation_id", "tenant_id"],
            ["index_generations.id", "index_generations.tenant_id"],
            name=op.f("fk_index_generation_documents_generation_id_index_generations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id", "tenant_id", "document_id"],
            [
                "document_versions.id",
                "document_versions.tenant_id",
                "document_versions.document_id",
            ],
            name=op.f("fk_index_generation_documents_document_version_id_document_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "generation_id", "document_id", name=op.f("pk_index_generation_documents")
        ),
    )

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("arq_job_id", sa.String(length=128)),
        sa.Column("status", sa.String(length=16), server_default="staged", nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.String(length=1000)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('staged', 'queued', 'processing', 'succeeded', 'failed')",
            name=op.f("ck_ingestion_jobs_valid_status"),
        ),
        sa.CheckConstraint("attempt >= 0", name=op.f("ck_ingestion_jobs_attempt_nonnegative")),
        sa.ForeignKeyConstraint(
            ["document_version_id", "tenant_id"],
            ["document_versions.id", "document_versions.tenant_id"],
            name=op.f("fk_ingestion_jobs_document_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingestion_jobs")),
        sa.UniqueConstraint(
            "document_version_id", name=op.f("uq_ingestion_jobs_document_version_id")
        ),
    )
    op.create_index("ix_ingestion_jobs_reconcile", "ingestion_jobs", ["status", "heartbeat_at"])


def downgrade() -> None:
    op.drop_index("ix_ingestion_jobs_reconcile", table_name="ingestion_jobs")
    op.drop_table("ingestion_jobs")
    op.drop_table("index_generation_documents")
    op.drop_constraint("fk_tenants_active_generation", "tenants", type_="foreignkey")
    op.drop_constraint("fk_documents_active_version", "documents", type_="foreignkey")
    op.drop_constraint("fk_document_versions_generation", "document_versions", type_="foreignkey")
    op.drop_index("ix_index_generations_tenant_created", table_name="index_generations")
    op.drop_table("index_generations")
    op.drop_index("ix_chunks_content_sha256", table_name="chunks")
    op.drop_index("ix_chunks_tenant_document_version", table_name="chunks")
    op.drop_table("chunks")
    op.drop_index("ix_sections_tenant_document", table_name="sections")
    op.drop_table("sections")
    op.drop_index("ix_document_versions_tenant_status", table_name="document_versions")
    op.drop_table("document_versions")
    op.drop_column("tenants", "active_index_generation_id")
