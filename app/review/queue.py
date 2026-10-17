"""The review queue: pending (or failed) files with filters and keyset pagination."""

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sqlalchemy import ColumnElement, and_, exists, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models_dedup import SimilarPair
from app.db.models_files import File, FileSource, FileStatus, MediaKind
from app.dedup.views import SimilarBadges, similar_badges
from app.storage.paths import is_valid_batch_id

QueueOrder = Literal["oldest", "newest"]
QUEUE_STATUSES = (FileStatus.pending_review, FileStatus.error)
SIMILAR_STATUSES = (FileStatus.archived, FileStatus.pending_review, FileStatus.discarded)
MAX_LIMIT = 500


class QueueError(ValueError):
    pass


@dataclass(frozen=True)
class QueueFilters:
    status: FileStatus = FileStatus.pending_review
    media_kinds: tuple[MediaKind, ...] = ()
    source_keys: tuple[str, ...] = ()
    batch: str | None = None
    has_similar: bool | None = None  # undismissed pair with an archived/pending/discarded file
    similar_to_discarded: bool | None = None  # undismissed pair with a discarded file
    order: QueueOrder = "oldest"

    def __post_init__(self) -> None:
        if self.status not in QUEUE_STATUSES:
            raise QueueError("status must be pending_review or error")
        if self.batch is not None and not is_valid_batch_id(self.batch):
            raise QueueError(f"invalid batch id: {self.batch!r}")


@dataclass
class QueuePage:
    files: list[File]
    badges: dict[int, SimilarBadges]
    total: int  # files matching the filters, all pages
    next_cursor: str | None = None
    items: list[dict[str, object]] = field(default_factory=list)


def encode_cursor(file: File) -> str:
    raw = f"{file.collected_at.isoformat()}|{file.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        at, file_id = raw.rsplit("|", 1)
        return datetime.fromisoformat(at), int(file_id)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise QueueError("invalid cursor") from exc


def _similar_exists(statuses: tuple[FileStatus, ...]) -> ColumnElement[bool]:
    other = aliased(File)
    return exists(
        select(1)
        .select_from(SimilarPair)
        .join(
            other,
            or_(
                and_(SimilarPair.file_a_id == File.id, other.id == SimilarPair.file_b_id),
                and_(SimilarPair.file_b_id == File.id, other.id == SimilarPair.file_a_id),
            ),
        )
        .where(SimilarPair.dismissed.is_(False), other.status.in_(statuses))
    )


def _conditions(filters: QueueFilters) -> list[ColumnElement[bool]]:
    where: list[ColumnElement[bool]] = [File.status == filters.status]
    if filters.media_kinds:
        where.append(File.media_kind.in_(filters.media_kinds))
    if filters.source_keys:
        where.append(
            exists(
                select(1).where(
                    FileSource.file_id == File.id, FileSource.source_key.in_(filters.source_keys)
                )
            )
        )
    if filters.batch:
        where.append(File.staging_path.startswith(f"staging/{filters.batch}/", autoescape=True))
    if filters.has_similar is not None:
        cond = _similar_exists(SIMILAR_STATUSES)
        where.append(cond if filters.has_similar else ~cond)
    if filters.similar_to_discarded is not None:
        cond = _similar_exists((FileStatus.discarded,))
        where.append(cond if filters.similar_to_discarded else ~cond)
    return where


def queue_item(file: File, badge: SimilarBadges | None) -> dict[str, object]:
    return {
        "id": file.id,
        "status": file.status.value,
        "original_filename": file.original_filename,
        "media_kind": file.media_kind.value,
        "mime": file.mime,
        "ext": file.ext,
        "size_bytes": file.size_bytes,
        "width": file.width,
        "height": file.height,
        "duration_ms": file.duration_ms,
        "collected_at": file.collected_at.isoformat(),
        "thumb_url": f"/media/{file.id}/thumb" if file.has_thumb else None,
        "original_url": f"/media/{file.id}/original",
        "error_message": file.error_message,
        "similar": {
            "kept": badge.kept if badge else [],
            "discarded": badge.discarded if badge else [],
        },
    }


async def review_queue(
    session: AsyncSession,
    filters: QueueFilters,
    cursor: str | None = None,
    limit: int = 100,
) -> QueuePage:
    limit = max(1, min(limit, MAX_LIMIT))
    where = _conditions(filters)
    total = await session.scalar(select(func.count()).select_from(File).where(*where)) or 0
    key = tuple_(File.collected_at, File.id)
    stmt = select(File).where(*where)
    if cursor:
        position = tuple_(*decode_cursor(cursor))
        stmt = stmt.where(key > position if filters.order == "oldest" else key < position)
    if filters.order == "oldest":
        stmt = stmt.order_by(File.collected_at, File.id)
    else:
        stmt = stmt.order_by(File.collected_at.desc(), File.id.desc())
    rows = list((await session.scalars(stmt.limit(limit + 1))).all())
    files, more = rows[:limit], len(rows) > limit
    badges = await similar_badges(session, [f.id for f in files])
    return QueuePage(
        files=files,
        badges=badges,
        total=total,
        next_cursor=encode_cursor(files[-1]) if more and files else None,
        items=[queue_item(f, badges.get(f.id)) for f in files],
    )
