"""review decisions: review_actions

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REVIEW_ACTION_KIND = postgresql.ENUM(
    "archive", "discard", "replace", name="review_action_kind", create_type=False
)
FILE_STATUS = postgresql.ENUM(name="file_status", create_type=False)


def upgrade() -> None:
    REVIEW_ACTION_KIND.create(op.get_bind(), checkfirst=False)
    op.create_table(
        "review_actions",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column(
            "file_id",
            sa.BigInteger,
            sa.ForeignKey("files.id", name="fk_review_actions_file_id_files", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", REVIEW_ACTION_KIND, nullable=False),
        sa.Column("previous_status", FILE_STATUS, nullable=False),
        sa.Column(
            "previous_folder_id",
            sa.BigInteger,
            sa.ForeignKey(
                "folders.id",
                name="fk_review_actions_previous_folder_id_folders",
                ondelete="SET NULL",
            ),
        ),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("undone", sa.Boolean, nullable=False, server_default=sa.false()),
        # Not in section 6: links the two rows of one replace; state needed to undo exactly.
        sa.Column("group_id", sa.Uuid),
        sa.Column(
            "detail",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_actions"),
    )
    op.create_index("ix_review_actions_file_id_id", "review_actions", ["file_id", "id"])
    op.create_index(
        "ix_review_actions_group_id",
        "review_actions",
        ["group_id"],
        postgresql_where=sa.text("group_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("review_actions")
    REVIEW_ACTION_KIND.drop(op.get_bind(), checkfirst=False)
