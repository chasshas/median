"""crawler add-on framework: crawl_sources, job_runs and their foreign keys

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_KIND = postgresql.ENUM(
    "crawl",
    "rehash",
    "integrity",
    "backup",
    "update_tools",
    "purge_trash",
    name="job_kind",
    create_type=False,
)
JOB_STATUS = postgresql.ENUM(
    "queued", "running", "succeeded", "failed", "cancelled", name="job_status", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    JOB_KIND.create(bind, checkfirst=False)
    JOB_STATUS.create(bind, checkfirst=False)

    op.create_table(
        "crawl_sources",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("addon_key", sa.Text, nullable=False),
        sa.Column(
            "config", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("schedule", sa.Text),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "default_folder_id",
            sa.BigInteger,
            sa.ForeignKey(
                "folders.id", name="fk_crawl_sources_default_folder_id_folders", ondelete="SET NULL"
            ),
        ),
        sa.Column(
            "default_tags",
            postgresql.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        # Not in section 6: the per-source runtime limit of the phase 5 brief.
        sa.Column("max_runtime_minutes", sa.Integer, nullable=False, server_default="120"),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_crawl_sources"),
        sa.CheckConstraint("max_runtime_minutes > 0", name="ck_crawl_sources_max_runtime"),
    )

    op.create_table(
        "job_runs",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column(
            "source_id",
            sa.BigInteger,
            sa.ForeignKey(
                "crawl_sources.id", name="fk_job_runs_source_id_crawl_sources", ondelete="SET NULL"
            ),
        ),
        sa.Column("kind", JOB_KIND, nullable=False),
        sa.Column("status", JOB_STATUS, nullable=False, server_default="queued"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("stats", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("log_path", sa.Text),
        sa.Column("error", sa.Text),
        # Not in section 6: when the run was queued, the cancel flag, the Procrastinate job.
        sa.Column(
            "queued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("queue_job_id", sa.BigInteger),
        sa.PrimaryKeyConstraint("id", name="pk_job_runs"),
    )
    op.create_index("ix_job_runs_source_id", "job_runs", ["source_id"])
    op.create_index("ix_job_runs_queued_at", "job_runs", ["queued_at"])
    # A source never has two queued or running runs.
    op.create_index(
        "uq_job_runs_active_source",
        "job_runs",
        ["source_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running') AND source_id IS NOT NULL"),
    )

    op.create_foreign_key(
        "fk_file_sources_source_id_crawl_sources",
        "file_sources",
        "crawl_sources",
        ["source_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_file_sources_job_run_id_job_runs",
        "file_sources",
        "job_runs",
        ["job_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_ingest_events_job_run_id_job_runs",
        "ingest_events",
        "job_runs",
        ["job_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_file_sources_source_id", "file_sources", ["source_id"])
    op.create_index("ix_file_sources_job_run_id", "file_sources", ["job_run_id"])


def downgrade() -> None:
    op.drop_index("ix_file_sources_job_run_id", "file_sources")
    op.drop_index("ix_file_sources_source_id", "file_sources")
    op.drop_constraint("fk_ingest_events_job_run_id_job_runs", "ingest_events", type_="foreignkey")
    op.drop_constraint("fk_file_sources_job_run_id_job_runs", "file_sources", type_="foreignkey")
    op.drop_constraint(
        "fk_file_sources_source_id_crawl_sources", "file_sources", type_="foreignkey"
    )
    op.drop_table("job_runs")
    op.drop_table("crawl_sources")
    JOB_STATUS.drop(op.get_bind(), checkfirst=False)
    JOB_KIND.drop(op.get_bind(), checkfirst=False)
