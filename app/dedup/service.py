"""Perceptual hashes, similar-file matching and similar_pairs maintenance (section 8)."""

import asyncio
import logging
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models_dedup import SimilarMethod, SimilarPair, VideoFrameHash
from app.db.models_files import File, FileStatus, MediaKind
from app.dedup.hashing import (
    hamming,
    phash_image_file,
    to_signed,
    to_unsigned,
    video_frame_hashes,
)
from app.dedup.index import BruteForceIndex
from app.dedup.settings import DedupSettings, get_dedup_settings
from app.storage import DataPaths

logger = logging.getLogger(__name__)

# Serializes "store hashes, find matches, write pairs" so two files ingested at the same time
# still see each other. Held until the transaction ends.
DEDUP_LOCK_KEY = 0x6D656469616E02  # "median" + phase 2
MIN_DURATION_TOLERANCE_MS = 1000
DURATION_TOLERANCE_RATIO = 0.03
MIN_MATCHING_FRAMES = 3
INSERT_CHUNK = 1000


@dataclass(frozen=True)
class Match:
    other_id: int
    method: SimilarMethod
    distance: int


def ordered(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


async def lock_dedup(session: AsyncSession) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": DEDUP_LOCK_KEY})


# --- hashing ---------------------------------------------------------------------------------


async def compute_hashes(session: AsyncSession, paths: DataPaths, file: File) -> bool:
    """Compute and store the file's perceptual hashes from its original.

    Returns False when the file kind has no perceptual hash or the original is gone.
    Exceptions from decoding propagate to the caller.
    """
    if file.media_kind not in (MediaKind.image, MediaKind.video):
        return False
    original = paths.original_path(file.status.value, file.staging_path, file.sha_hex, file.ext)
    if original is None or not original.is_file():
        return False
    if file.media_kind is MediaKind.image:
        file.phash = to_signed(await asyncio.to_thread(phash_image_file, original))
        await session.execute(delete(VideoFrameHash).where(VideoFrameHash.file_id == file.id))
        return True
    file.phash = None
    await session.execute(delete(VideoFrameHash).where(VideoFrameHash.file_id == file.id))
    if not file.duration_ms:
        return False
    frames = await video_frame_hashes(original, file.duration_ms)
    if not frames:
        raise RuntimeError("ffmpeg produced no frames to hash")
    session.add_all(
        VideoFrameHash(file_id=file.id, frame_index=index, phash=to_signed(value))
        for index, value in frames.items()
    )
    await session.flush()
    return True


# --- matching --------------------------------------------------------------------------------


def duration_bounds(duration_ms: int) -> tuple[float, float]:
    """Range of durations that can be within max(3 percent, 1 s) of duration_ms.

    The 3 percent are taken of the longer of the two durations, so the rule is symmetric.
    """
    r = DURATION_TOLERANCE_RATIO
    low = min(duration_ms - MIN_DURATION_TOLERANCE_MS, duration_ms * (1 - r))
    high = max(duration_ms + MIN_DURATION_TOLERANCE_MS, duration_ms / (1 - r))
    return low, high


def compare_frames(a: dict[int, int], b: dict[int, int], threshold: int) -> int | None:
    """Median frame distance when the videos match, else None.

    Frames are compared by frame_index. With 5 common frames at least 3 must be within the
    threshold; with fewer common frames (short videos) min(3, common) must be.
    """
    common = sorted(a.keys() & b.keys())
    if not common:
        return None
    distances = [hamming(a[i], b[i]) for i in common]
    needed = min(MIN_MATCHING_FRAMES, len(common))
    if sum(d <= threshold for d in distances) < needed:
        return None
    return int(statistics.median_low(distances))


async def load_image_index(session: AsyncSession) -> BruteForceIndex:
    rows = await session.execute(select(File.id, File.phash).where(File.phash.is_not(None)))
    return BruteForceIndex.from_signed(rows.all())


async def frame_hashes(session: AsyncSession, file_ids: Iterable[int]) -> dict[int, dict[int, int]]:
    """Unsigned frame hashes per file_id and frame_index."""
    ids = list(file_ids)
    found: dict[int, dict[int, int]] = {}
    if not ids:
        return found
    rows = await session.execute(
        select(VideoFrameHash.file_id, VideoFrameHash.frame_index, VideoFrameHash.phash).where(
            VideoFrameHash.file_id.in_(ids)
        )
    )
    for file_id, index, value in rows:
        found.setdefault(file_id, {})[index] = to_unsigned(value)
    return found


async def video_candidates(session: AsyncSession, file_id: int, duration_ms: int) -> list[int]:
    """Hashed videos whose duration differs from duration_ms by at most max(3 %, 1 s)."""
    low, high = duration_bounds(duration_ms)
    longer = func.greatest(File.duration_ms, duration_ms)
    tolerance = func.greatest(MIN_DURATION_TOLERANCE_MS, longer * DURATION_TOLERANCE_RATIO)
    has_frames = select(VideoFrameHash.file_id).where(VideoFrameHash.file_id == File.id).exists()
    result = await session.scalars(
        select(File.id).where(
            File.media_kind == MediaKind.video,
            File.id != file_id,
            File.duration_ms.between(int(low), int(high) + 1),
            func.abs(File.duration_ms - duration_ms) <= tolerance,
            has_frames,
        )
    )
    return list(result.all())


async def find_video_matches(
    session: AsyncSession, file_id: int, duration_ms: int | None, threshold: int
) -> list[Match]:
    if not duration_ms:
        return []
    candidates = await video_candidates(session, file_id, duration_ms)
    hashes = await frame_hashes(session, [file_id, *candidates])
    own = hashes.get(file_id)
    if not own:
        return []
    matches = []
    for other in candidates:
        distance = compare_frames(own, hashes.get(other, {}), threshold)
        if distance is not None:
            matches.append(Match(other, SimilarMethod.video_frames, distance))
    return matches


async def find_matches(
    session: AsyncSession,
    file: File,
    settings: DedupSettings,
    image_index: BruteForceIndex | None = None,
) -> list[Match]:
    """Files in any status that are perceptually similar to file, from stored hashes."""
    if file.media_kind is MediaKind.image and file.phash is not None:
        index = image_index if image_index is not None else await load_image_index(session)
        return [
            Match(other, SimilarMethod.image_phash, distance)
            for other, distance in index.query(
                to_unsigned(file.phash), settings.image_phash_threshold, exclude_id=file.id
            )
        ]
    if file.media_kind is MediaKind.video:
        return await find_video_matches(
            session, file.id, file.duration_ms, settings.video_frame_threshold
        )
    return []


# --- similar_pairs ---------------------------------------------------------------------------


async def save_pairs(
    session: AsyncSession, file_id: int, matches: list[Match], *, prune: bool
) -> None:
    """Record matches for file_id. Existing pairs keep their dismissed flag.

    With prune, pairs of file_id that are no longer matches are deleted (dismissed or not)
    and the distance of existing pairs is refreshed.
    """
    if prune:
        keep = {(*ordered(file_id, m.other_id), m.method) for m in matches}
        existing = await session.execute(
            select(SimilarPair.file_a_id, SimilarPair.file_b_id, SimilarPair.method).where(
                or_(SimilarPair.file_a_id == file_id, SimilarPair.file_b_id == file_id)
            )
        )
        for a, b, method in existing:
            if (a, b, method) not in keep:
                await session.execute(
                    delete(SimilarPair).where(
                        SimilarPair.file_a_id == a,
                        SimilarPair.file_b_id == b,
                        SimilarPair.method == method,
                    )
                )
    for m in matches:
        a, b = ordered(file_id, m.other_id)
        stmt = insert(SimilarPair).values(
            file_a_id=a, file_b_id=b, method=m.method, distance=m.distance
        )
        if prune:
            stmt = stmt.on_conflict_do_update(
                constraint="pk_similar_pairs", set_={"distance": stmt.excluded.distance}
            )
        else:
            stmt = stmt.on_conflict_do_nothing(constraint="pk_similar_pairs")
        await session.execute(stmt)


async def compute_perceptual(
    session: AsyncSession, paths: DataPaths, file: File, settings: DedupSettings | None = None
) -> None:
    """Ingest hook: hash a new file and flag files similar to it.

    A file whose hashes cannot be computed stays reviewable; the reason is kept in
    metadata.perceptual_error.
    """
    settings = settings or get_dedup_settings()
    try:
        hashed = await compute_hashes(session, paths, file)
    except Exception as exc:
        logger.warning("perceptual hashing of file %s failed: %s", file.id, exc)
        file.metadata_ = {**(file.metadata_ or {}), "perceptual_error": f"{exc}"[:500]}
        return
    if not hashed:
        return
    if "perceptual_error" in (file.metadata_ or {}):
        file.metadata_ = {k: v for k, v in file.metadata_.items() if k != "perceptual_error"}
    await lock_dedup(session)
    await save_pairs(session, file.id, await find_matches(session, file, settings), prune=False)


async def dismiss_pair(session: AsyncSession, a: int, b: int, method: SimilarMethod) -> bool:
    """Hide a pair for good. Returns False when the pair does not exist."""
    a, b = ordered(a, b)
    result = await session.execute(
        update(SimilarPair)
        .where(SimilarPair.file_a_id == a, SimilarPair.file_b_id == b, SimilarPair.method == method)
        .values(dismissed=True)
        .returning(SimilarPair.file_a_id)
    )
    return result.first() is not None


# --- maintenance commands --------------------------------------------------------------------


@dataclass
class RehashStats:
    considered: int = 0
    hashed: int = 0
    no_original: int = 0
    not_hashable: int = 0
    errors: int = 0
    error_ids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "considered": self.considered,
            "hashed": self.hashed,
            "no_original": self.no_original,
            "not_hashable": self.not_hashable,
            "errors": self.errors,
            "error_ids": self.error_ids[:50],
        }


async def rehash(
    sessionmaker: async_sessionmaker[AsyncSession],
    paths: DataPaths,
    *,
    missing_only: bool = False,
    settings: DedupSettings | None = None,
) -> RehashStats:
    """Recompute hashes of image and video files that still have an original, and rebuild
    their pairs. Files in status processing are left to their ingest job.

    missing_only limits it to images without a pHash and videos without frame hashes.
    """
    settings = settings or get_dedup_settings()
    stats = RehashStats()
    query = select(File.id).where(
        File.media_kind.in_([MediaKind.image, MediaKind.video]),
        File.status != FileStatus.processing,
    )
    if missing_only:
        has_frames = (
            select(VideoFrameHash.file_id).where(VideoFrameHash.file_id == File.id).exists()
        )
        query = query.where(
            or_(
                (File.media_kind == MediaKind.image) & File.phash.is_(None),
                (File.media_kind == MediaKind.video) & ~has_frames,
            )
        )
    async with sessionmaker() as session:
        file_ids = list((await session.scalars(query.order_by(File.id))).all())

    for file_id in file_ids:
        stats.considered += 1
        async with sessionmaker() as session:
            file = await session.get(File, file_id, with_for_update=True)
            if file is None or file.status is FileStatus.processing:
                continue
            original = paths.original_path(
                file.status.value, file.staging_path, file.sha_hex, file.ext
            )
            if original is None or not original.is_file():
                stats.no_original += 1
                continue
            try:
                hashed = await compute_hashes(session, paths, file)
            except Exception as exc:
                logger.warning("rehash of file %s failed: %s", file_id, exc)
                await session.rollback()
                stats.errors += 1
                stats.error_ids.append(file_id)
                continue
            if not hashed:
                stats.not_hashable += 1
                await session.commit()
                continue
            await lock_dedup(session)
            matches = await find_matches(session, file, settings)
            await save_pairs(session, file_id, matches, prune=True)
            await session.commit()
            stats.hashed += 1
    return stats


@dataclass
class RecomputeStats:
    pairs: int = 0
    inserted: int = 0
    removed: int = 0
    dismissed_kept: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "pairs": self.pairs,
            "inserted": self.inserted,
            "removed": self.removed,
            "dismissed_kept": self.dismissed_kept,
        }


async def recompute_similar(
    sessionmaker: async_sessionmaker[AsyncSession], settings: DedupSettings | None = None
) -> RecomputeStats:
    """Rebuild similar_pairs from stored hashes with the current thresholds.

    Pairs that still match keep their dismissed flag; pairs that no longer match are deleted.
    """
    settings = settings or get_dedup_settings()
    stats = RecomputeStats()
    async with sessionmaker() as session:
        await lock_dedup(session)
        wanted: dict[tuple[int, int, SimilarMethod], int] = {}

        index = await load_image_index(session)
        for file_id, value in zip(index.ids.tolist(), index.hashes.tolist(), strict=True):
            for other, distance in index.query(
                value, settings.image_phash_threshold, exclude_id=file_id
            ):
                if other > file_id:
                    wanted[(file_id, other, SimilarMethod.image_phash)] = distance

        videos = await session.execute(
            select(File.id, File.duration_ms).where(
                File.media_kind == MediaKind.video,
                File.duration_ms.is_not(None),
                select(VideoFrameHash.file_id).where(VideoFrameHash.file_id == File.id).exists(),
            )
        )
        for file_id, duration_ms in videos.all():
            for m in await find_video_matches(
                session, file_id, duration_ms, settings.video_frame_threshold
            ):
                if m.other_id > file_id:
                    wanted[(file_id, m.other_id, m.method)] = m.distance

        existing = {
            (a, b, method): (distance, dismissed)
            for a, b, method, distance, dismissed in (
                await session.execute(
                    select(
                        SimilarPair.file_a_id,
                        SimilarPair.file_b_id,
                        SimilarPair.method,
                        SimilarPair.distance,
                        SimilarPair.dismissed,
                    )
                )
            )
        }
        for key, (distance, dismissed) in existing.items():
            a, b, method = key
            where = (
                SimilarPair.file_a_id == a,
                SimilarPair.file_b_id == b,
                SimilarPair.method == method,
            )
            if key not in wanted:
                await session.execute(delete(SimilarPair).where(*where))
                stats.removed += 1
                continue
            if dismissed:
                stats.dismissed_kept += 1
            if wanted[key] != distance:
                await session.execute(
                    update(SimilarPair).where(*where).values(distance=wanted[key])
                )
        new = [
            {"file_a_id": a, "file_b_id": b, "method": method, "distance": distance}
            for (a, b, method), distance in wanted.items()
            if (a, b, method) not in existing
        ]
        for start in range(0, len(new), INSERT_CHUNK):
            await session.execute(insert(SimilarPair).values(new[start : start + INSERT_CHUNK]))
        stats.inserted = len(new)
        stats.pairs = len(wanted)
        await session.commit()
    return stats
