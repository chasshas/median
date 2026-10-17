"""perceptual duplicate detection: video_frame_hashes and similar_pairs

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIMILAR_METHOD = postgresql.ENUM(
    "image_phash", "video_frames", name="similar_method", create_type=False
)


def upgrade() -> None:
    SIMILAR_METHOD.create(op.get_bind(), checkfirst=False)

    op.create_table(
        "video_frame_hashes",
        sa.Column(
            "file_id",
            sa.BigInteger,
            sa.ForeignKey(
                "files.id", name="fk_video_frame_hashes_file_id_files", ondelete="CASCADE"
            ),
            nullable=False,
        ),
        sa.Column("frame_index", sa.SmallInteger, nullable=False),
        sa.Column("phash", sa.BigInteger, nullable=False),
        sa.PrimaryKeyConstraint("file_id", "frame_index", name="pk_video_frame_hashes"),
        sa.CheckConstraint("frame_index BETWEEN 0 AND 4", name="ck_video_frame_hashes_frame_index"),
    )

    op.create_table(
        "similar_pairs",
        sa.Column(
            "file_a_id",
            sa.BigInteger,
            sa.ForeignKey("files.id", name="fk_similar_pairs_file_a_id_files", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "file_b_id",
            sa.BigInteger,
            sa.ForeignKey("files.id", name="fk_similar_pairs_file_b_id_files", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("method", SIMILAR_METHOD, nullable=False),
        sa.Column("distance", sa.SmallInteger, nullable=False),
        sa.Column("dismissed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("file_a_id", "file_b_id", "method", name="pk_similar_pairs"),
        sa.CheckConstraint("file_a_id < file_b_id", name="ck_similar_pairs_ordered"),
    )
    # The primary key serves lookups by file_a_id; this one serves lookups by file_b_id.
    op.create_index("ix_similar_pairs_file_b_id", "similar_pairs", ["file_b_id"])
    # Candidate selection for video matching filters videos by duration.
    op.create_index(
        "ix_files_video_duration",
        "files",
        ["duration_ms"],
        postgresql_where=sa.text("media_kind = 'video'"),
    )


def downgrade() -> None:
    op.drop_index("ix_files_video_duration", table_name="files")
    op.drop_table("similar_pairs")
    op.drop_table("video_frame_hashes")
    SIMILAR_METHOD.drop(op.get_bind(), checkfirst=False)
