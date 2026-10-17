"""Review decisions, kept so they can be undone (DECISIONS.md, phase 3)."""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models_files import FileStatus, pg_enum


class ReviewActionKind(enum.StrEnum):
    archive = "archive"
    discard = "discard"
    replace = "replace"


class ReviewAction(Base):
    __tablename__ = "review_actions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    file_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[ReviewActionKind] = mapped_column(
        pg_enum(ReviewActionKind, "review_action_kind"), nullable=False
    )
    previous_status: Mapped[FileStatus] = mapped_column(
        pg_enum(FileStatus, "file_status"), nullable=False
    )
    previous_folder_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("folders.id", ondelete="SET NULL")
    )
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    undone: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # The two rows of one replace share a group_id.
    group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # Previous staging_path and review timestamps, tag rows the action changed.
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        Index("ix_review_actions_file_id_id", "file_id", "id"),
        Index(
            "ix_review_actions_group_id",
            "group_id",
            postgresql_where=text("group_id IS NOT NULL"),
        ),
    )
