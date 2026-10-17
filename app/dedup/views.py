"""Read-side queries for similar files (file page, review badges, API)."""

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_dedup import SimilarPair
from app.db.models_files import File, FileStatus

KEPT_STATUSES = (FileStatus.archived, FileStatus.pending_review)


@dataclass
class SimilarFile:
    other_id: int
    status: str
    has_thumb: bool
    original_filename: str | None
    media_kind: str
    distance: int
    method: str
    dismissed: bool

    @property
    def thumb_url(self) -> str | None:
        return f"/media/{self.other_id}/thumb" if self.has_thumb else None

    def as_json(self) -> dict[str, object]:
        return {
            "file_id": self.other_id,
            "status": self.status,
            "thumb_url": self.thumb_url,
            "distance": self.distance,
            "method": self.method,
            "dismissed": self.dismissed,
        }


@dataclass
class SimilarBadges:
    """Undismissed similar files of one file, split by the other file's status."""

    kept: list[int] = field(default_factory=list)  # archived or pending review
    discarded: list[int] = field(default_factory=list)


def _directed_pairs():
    """similar_pairs seen from both ends: (file_id, other_id, method, distance, dismissed)."""
    forward = select(
        SimilarPair.file_a_id.label("file_id"),
        SimilarPair.file_b_id.label("other_id"),
        SimilarPair.method,
        SimilarPair.distance,
        SimilarPair.dismissed,
    )
    backward = select(
        SimilarPair.file_b_id.label("file_id"),
        SimilarPair.file_a_id.label("other_id"),
        SimilarPair.method,
        SimilarPair.distance,
        SimilarPair.dismissed,
    )
    return union_all(forward, backward).subquery("directed")


async def similar_files(session: AsyncSession, file_id: int) -> list[SimilarFile]:
    """Every pair of file_id (dismissed ones too), nearest first."""
    d = _directed_pairs()
    rows = await session.execute(
        select(
            d.c.other_id,
            File.status,
            File.has_thumb,
            File.original_filename,
            File.media_kind,
            d.c.distance,
            d.c.method,
            d.c.dismissed,
        )
        .join(File, File.id == d.c.other_id)
        .where(d.c.file_id == file_id)
        .order_by(d.c.dismissed, d.c.distance, d.c.other_id)
    )
    return [
        SimilarFile(
            other_id=other_id,
            status=status.value,
            has_thumb=has_thumb,
            original_filename=name,
            media_kind=kind.value,
            distance=distance,
            method=method.value,
            dismissed=dismissed,
        )
        for other_id, status, has_thumb, name, kind, distance, method, dismissed in rows.all()
    ]


async def similar_badges(
    session: AsyncSession, file_ids: Iterable[int]
) -> dict[int, SimilarBadges]:
    """Badge data for the review grid: undismissed similar files per file id."""
    ids = list(file_ids)
    badges: dict[int, SimilarBadges] = {}
    if not ids:
        return badges
    d = _directed_pairs()
    rows = await session.execute(
        select(d.c.file_id, d.c.other_id, File.status)
        .distinct()
        .join(File, File.id == d.c.other_id)
        .where(d.c.file_id.in_(ids), d.c.dismissed.is_(False))
        .order_by(d.c.file_id, d.c.other_id)
    )
    for file_id, other_id, status in rows.all():
        badge = badges.setdefault(file_id, SimilarBadges())
        if status is FileStatus.discarded:
            badge.discarded.append(other_id)
        elif status in KEPT_STATUSES:
            badge.kept.append(other_id)
    return badges
