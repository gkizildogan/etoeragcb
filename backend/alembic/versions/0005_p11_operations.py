"""Record generation-safe payload garbage collection.

Revision ID: 0005_p11_operations
Revises: 0004_p4_staged_ingestion
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_p11_operations"
down_revision: str | None = "0004_p4_staged_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_versions",
        sa.Column("garbage_collected_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_document_versions_gc_candidates",
        "document_versions",
        ["tenant_id", "status", "created_at", "garbage_collected_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_document_versions_gc_candidates", table_name="document_versions")
    op.drop_column("document_versions", "garbage_collected_at")
