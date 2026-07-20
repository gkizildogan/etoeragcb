"""Establish the migration baseline before the P2 durable schema.

Revision ID: 0001_p1_baseline
Revises:
"""

from collections.abc import Sequence

revision: str = "0001_p1_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
