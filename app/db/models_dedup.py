"""Perceptual hashes of videos and similar-file pairs (ARCHITECTURE.md sections 6 and 8)."""

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models_files import pg_enum


class SimilarMethod(enum.StrEnum):
    image_phash = "image_phash"
    video_frames = "video_frames"


class VideoFrameHash(Base):
    __tablename__ = "video_frame_hashes"

    file_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("files.id", ondelete="CASCADE"), primary_key=True
    )
    frame_index: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    # 64-bit pHash stored as signed int64 (app.dedup.hashing.to_signed).
    phash: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint("frame_index BETWEEN 0 AND 4", name="ck_video_frame_hashes_frame_index"),
    )


class SimilarPair(Base):
    __tablename__ = "similar_pairs"

    file_a_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("files.id", ondelete="CASCADE"), primary_key=True
    )
    file_b_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("files.id", ondelete="CASCADE"), primary_key=True
    )
    method: Mapped[SimilarMethod] = mapped_column(
        pg_enum(SimilarMethod, "similar_method"), primary_key=True
    )
    distance: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    dismissed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("file_a_id < file_b_id", name="ck_similar_pairs_ordered"),
        Index("ix_similar_pairs_file_b_id", "file_b_id"),
    )
