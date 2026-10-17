"""Crawl sources and job runs (ARCHITECTURE.md sections 6 and 11)."""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models_files import pg_enum

DEFAULT_MAX_RUNTIME_MINUTES = 120


class JobKind(enum.StrEnum):
    crawl = "crawl"
    rehash = "rehash"
    integrity = "integrity"
    backup = "backup"
    update_tools = "update_tools"
    purge_trash = "purge_trash"


class JobStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


ACTIVE_STATUSES = (JobStatus.queued, JobStatus.running)


class CrawlSource(Base):
    __tablename__ = "crawl_sources"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    addon_key: Mapped[str] = mapped_column(Text, nullable=False)
    # The add-on's config without secret fields (those live under secrets/<source_id>/).
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    schedule: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    default_folder_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("folders.id", ondelete="SET NULL")
    )
    default_tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    max_runtime_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MAX_RUNTIME_MINUTES
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (CheckConstraint("max_runtime_minutes > 0", name="max_runtime"),)


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("crawl_sources.id", ondelete="SET NULL")
    )
    kind: Mapped[JobKind] = mapped_column(pg_enum(JobKind, "job_kind"), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        pg_enum(JobStatus, "job_status"), nullable=False, default=JobStatus.queued
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    log_path: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    queue_job_id: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        Index("ix_job_runs_source_id", "source_id"),
        Index("ix_job_runs_queued_at", "queued_at"),
        Index(
            "uq_job_runs_active_source",
            "source_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running') AND source_id IS NOT NULL"),
        ),
    )

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES
