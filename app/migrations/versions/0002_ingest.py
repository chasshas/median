"""files, sources, ingest events, folders and tags

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MEDIA_KIND = postgresql.ENUM(
    "image", "video", "audio", "other", name="media_kind", create_type=False
)
FILE_STATUS = postgresql.ENUM(
    "processing",
    "pending_review",
    "archived",
    "discarded",
    "error",
    name="file_status",
    create_type=False,
)
INGEST_OUTCOME = postgresql.ENUM(
    "new",
    "duplicate_exact",
    "skipped_known_remote",
    "forced_reingest",
    "error",
    name="ingest_outcome",
    create_type=False,
)
TAG_ORIGIN = postgresql.ENUM("crawler", "user", name="tag_origin", create_type=False)

ENUMS = (MEDIA_KIND, FILE_STATUS, INGEST_OUTCOME, TAG_ORIGIN)


def _id() -> sa.Column:
    return sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True)


def _now(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _jsonb(name: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))


def upgrade() -> None:
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind, checkfirst=False)

    op.create_table(
        "folders",
        _id(),
        sa.Column(
            "parent_id",
            sa.BigInteger,
            sa.ForeignKey("folders.id", name="fk_folders_parent_id_folders"),
        ),
        sa.Column("name", sa.Text, nullable=False),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_folders"),
    )
    op.create_index(
        "uq_folders_parent_name",
        "folders",
        [sa.text("coalesce(parent_id, 0)"), sa.text("lower(name)")],
        unique=True,
    )
    op.execute("INSERT INTO folders (parent_id, name) VALUES (NULL, 'Inbox')")

    op.create_table(
        "files",
        _id(),
        sa.Column("sha256", sa.LargeBinary(32), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("mime", sa.Text),
        sa.Column("ext", sa.Text),
        sa.Column("media_kind", MEDIA_KIND, nullable=False),
        sa.Column("width", sa.Integer),
        sa.Column("height", sa.Integer),
        sa.Column("duration_ms", sa.BigInteger),
        sa.Column("status", FILE_STATUS, nullable=False),
        sa.Column("error_message", sa.Text),
        sa.Column("staging_path", sa.Text),
        sa.Column("has_thumb", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("phash", sa.BigInteger),
        sa.Column("original_filename", sa.Text),
        sa.Column("title", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column(
            "folder_id",
            sa.BigInteger,
            sa.ForeignKey("folders.id", name="fk_files_folder_id_folders"),
        ),
        _now("collected_at"),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("discarded_at", sa.DateTime(timezone=True)),
        _jsonb("metadata"),
        sa.PrimaryKeyConstraint("id", name="pk_files"),
        sa.UniqueConstraint("sha256", name="uq_files_sha256"),
    )
    for column in ("status", "folder_id", "collected_at", "archived_at", "media_kind"):
        op.create_index(f"ix_files_{column}", "files", [column])

    op.create_table(
        "file_sources",
        _id(),
        sa.Column(
            "file_id",
            sa.BigInteger,
            sa.ForeignKey("files.id", name="fk_file_sources_file_id_files", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_key", sa.Text, nullable=False),
        sa.Column("remote_id", sa.Text),
        sa.Column("url", sa.Text),
        # Foreign keys to crawl_sources and job_runs are added in phase 5.
        sa.Column("source_id", sa.BigInteger),
        sa.Column("job_run_id", sa.BigInteger),
        _now("fetched_at"),
        _jsonb("raw_metadata"),
        sa.PrimaryKeyConstraint("id", name="pk_file_sources"),
    )
    op.create_index("ix_file_sources_file_id", "file_sources", ["file_id"])
    op.create_index(
        "uq_file_sources_source_key_remote_id",
        "file_sources",
        ["source_key", "remote_id"],
        unique=True,
        postgresql_where=sa.text("remote_id IS NOT NULL"),
    )

    op.create_table(
        "ingest_events",
        _id(),
        _now("at"),
        sa.Column("outcome", INGEST_OUTCOME, nullable=False),
        sa.Column(
            "file_id",
            sa.BigInteger,
            sa.ForeignKey("files.id", name="fk_ingest_events_file_id_files", ondelete="SET NULL"),
        ),
        sa.Column("source_key", sa.Text, nullable=False),
        sa.Column("remote_id", sa.Text),
        sa.Column("url", sa.Text),
        sa.Column("job_run_id", sa.BigInteger),
        sa.Column("batch_id", sa.Text),
        sa.Column("detail", sa.Text),
        sa.PrimaryKeyConstraint("id", name="pk_ingest_events"),
    )
    for column in ("batch_id", "job_run_id", "file_id"):
        op.create_index(f"ix_ingest_events_{column}", "ingest_events", [column])

    op.create_table(
        "tags",
        _id(),
        sa.Column("name", sa.Text, nullable=False),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_tags"),
        sa.UniqueConstraint("name", name="uq_tags_name"),
    )

    op.create_table(
        "file_tags",
        sa.Column(
            "file_id",
            sa.BigInteger,
            sa.ForeignKey("files.id", name="fk_file_tags_file_id_files", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tag_id",
            sa.BigInteger,
            sa.ForeignKey("tags.id", name="fk_file_tags_tag_id_tags", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("origin", TAG_ORIGIN, nullable=False),
        sa.Column("removed", sa.Boolean, nullable=False, server_default=sa.false()),
        _now("created_at"),
        sa.PrimaryKeyConstraint("file_id", "tag_id", name="pk_file_tags"),
    )
    op.create_index(
        "ix_file_tags_tag_id_file_id",
        "file_tags",
        ["tag_id", "file_id"],
        postgresql_where=sa.text("removed = false"),
    )


def downgrade() -> None:
    for table in ("file_tags", "tags", "ingest_events", "file_sources", "files", "folders"):
        op.drop_table(table)
    bind = op.get_bind()
    for enum in reversed(ENUMS):
        enum.drop(bind, checkfirst=False)
