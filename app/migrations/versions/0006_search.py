"""search: saved_searches and sort indexes on files

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "saved_searches",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("query", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_saved_searches"),
        sa.UniqueConstraint("name", name="uq_saved_searches_name"),
    )
    op.create_index(
        "ix_files_status_archived_at_id",
        "files",
        ["status", sa.text("archived_at DESC"), "id"],
    )
    op.create_index(
        "ix_files_status_collected_at_id",
        "files",
        ["status", sa.text("collected_at DESC"), "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_files_status_collected_at_id", table_name="files")
    op.drop_index("ix_files_status_archived_at_id", table_name="files")
    op.drop_table("saved_searches")
