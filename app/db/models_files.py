"""Files, their sources and the ingest event log (ARCHITECTURE.md section 6)."""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MediaKind(enum.StrEnum):
    image = "image"
    video = "video"
    audio = "audio"
    other = "other"


class FileStatus(enum.StrEnum):
    processing = "processing"
    pending_review = "pending_review"
    archived = "archived"
    discarded = "discarded"
    error = "error"


class IngestOutcome(enum.StrEnum):
    new = "new"
    duplicate_exact = "duplicate_exact"
    skipped_known_remote = "skipped_known_remote"
    forced_reingest = "forced_reingest"
    error = "error"


def pg_enum(cls: type[enum.Enum], name: str) -> Enum:
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e], create_type=False)


class File(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    sha256: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime: Mapped[str | None] = mapped_column(Text)
    ext: Mapped[str | None] = mapped_column(Text)
    media_kind: Mapped[MediaKind] = mapped_column(
        pg_enum(MediaKind, "media_kind"), nullable=False, default=MediaKind.other
    )
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[FileStatus] = mapped_column(pg_enum(FileStatus, "file_status"), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    staging_path: Mapped[str | None] = mapped_column(Text)
    has_thumb: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    phash: Mapped[int | None] = mapped_column(BigInteger)
    original_filename: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    folder_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("folders.id"))
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        Index("ix_files_status", "status"),
        Index("ix_files_folder_id", "folder_id"),
        Index("ix_files_collected_at", "collected_at"),
        Index("ix_files_archived_at", "archived_at"),
        Index("ix_files_media_kind", "media_kind"),
        Index(
            "ix_files_video_duration",
            "duration_ms",
            postgresql_where=text("media_kind = 'video'"),
        ),
    )

    @property
    def sha_hex(self) -> str:
        return self.sha256.hex()


class FileSource(Base):
    __tablename__ = "file_sources"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    file_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    source_key: Mapped[str] = mapped_column(Text, nullable=False)
    remote_id: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    # Foreign keys to crawl_sources and job_runs arrive in phase 5.
    source_id: Mapped[int | None] = mapped_column(BigInteger)
    job_run_id: Mapped[int | None] = mapped_column(BigInteger)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        Index("ix_file_sources_file_id", "file_id"),
        Index(
            "uq_file_sources_source_key_remote_id",
            "source_key",
            "remote_id",
            unique=True,
            postgresql_where=text("remote_id IS NOT NULL"),
        ),
    )


class IngestEvent(Base):
    __tablename__ = "ingest_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    outcome: Mapped[IngestOutcome] = mapped_column(
        pg_enum(IngestOutcome, "ingest_outcome"), nullable=False
    )
    file_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("files.id", ondelete="SET NULL")
    )
    source_key: Mapped[str] = mapped_column(Text, nullable=False)
    remote_id: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    job_run_id: Mapped[int | None] = mapped_column(BigInteger)
    batch_id: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_ingest_events_batch_id", "batch_id"),
        Index("ix_ingest_events_job_run_id", "job_run_id"),
        Index("ix_ingest_events_file_id", "file_id"),
    )
