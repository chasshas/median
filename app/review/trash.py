"""purge_trash: delete trashed originals older than TRASH_RETENTION_DAYS (ARCHITECTURE.md 7).

Discarded rows, hashes, thumbnails, sources and tags stay forever, so discarded content keeps
counting as a duplicate; only the original under trash/ goes, and with it the option to undo.
"""

import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import job_sessionmaker
from app.db.models_files import File, FileStatus
from app.storage import DataPaths
from app.storage.trash import purge_from_trash

logger = logging.getLogger(__name__)


@dataclass
class PurgeStats:
    expired: int = 0  # discarded files past the retention period
    purged: int = 0  # trash originals deleted by this run
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


async def purge_trash(
    session: AsyncSession,
    paths: DataPaths,
    retention_days: int,
    now: datetime | None = None,
) -> PurgeStats:
    """Delete trash originals of files discarded more than retention_days ago."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    rows = await session.execute(
        select(File.id, File.sha256, File.ext).where(
            File.status == FileStatus.discarded, File.discarded_at < cutoff
        )
    )
    stats = PurgeStats()
    for file_id, sha, ext in rows.all():
        stats.expired += 1
        try:
            if purge_from_trash(paths, sha.hex(), ext):
                stats.purged += 1
        except OSError:
            logger.exception("purging the trash original of file %s failed", file_id)
            stats.errors += 1
    return stats


async def run_purge_trash(database_url: str, paths: DataPaths, retention_days: int) -> PurgeStats:
    """purge_trash in its own engine and session (the job and the CLI)."""
    async with job_sessionmaker(database_url) as sessionmaker, sessionmaker() as session:
        return await purge_trash(session, paths, retention_days)
