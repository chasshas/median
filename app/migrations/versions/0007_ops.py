"""operations: app_settings, schedule_state, maintenance job kinds

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("value", postgresql.JSONB, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("key", name="pk_app_settings"),
    )
    op.create_table(
        "schedule_state",
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name", name="pk_schedule_state"),
    )
    # New values cannot be used in this transaction; nothing below does.
    op.execute("ALTER TYPE job_kind ADD VALUE IF NOT EXISTS 'recompute_similar'")
    op.execute("ALTER TYPE job_kind ADD VALUE IF NOT EXISTS 'housekeeping'")
    # One queued or running maintenance run per kind (crawls are limited per source).
    op.create_index(
        "uq_job_runs_active_kind",
        "job_runs",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running') AND kind <> 'crawl'"),
    )
    op.create_index("ix_job_runs_kind_status", "job_runs", ["kind", "status"])


def downgrade() -> None:
    # Enum values stay: PostgreSQL cannot drop them.
    op.drop_index("ix_job_runs_kind_status", table_name="job_runs")
    op.drop_index("uq_job_runs_active_kind", table_name="job_runs")
    op.drop_table("schedule_state")
    op.drop_table("app_settings")
