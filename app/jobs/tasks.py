"""Task registration. Each task delegates to a service; keep task bodies thin."""

import logging
from typing import Any

from app.config import get_settings
from app.db.engine import job_sessionmaker
from app.jobs.app import job_app
from app.storage import get_paths

logger = logging.getLogger(__name__)


@job_app.task(name="ping", queue="default")
async def ping(message: str = "ping") -> str:
    logger.info("ping task received %r, replying pong", message)
    return "pong"


@job_app.task(name="process_ingest", queue="ingest")
async def process_ingest(
    staging_path: str, origin: dict[str, Any], original_filename: str, batch_id: str
) -> str:
    from app.ingest import process
    from app.ingest.origin import IngestOrigin

    async with job_sessionmaker(get_settings().database_url) as sessionmaker:
        outcome = await process.process_ingest(
            sessionmaker,
            get_paths(),
            staging_path,
            IngestOrigin.model_validate(origin),
            original_filename,
            batch_id,
        )
    return outcome.value


@job_app.task(name="retry_file", queue="ingest")
async def retry_file(file_id: int) -> str:
    from app.ingest import process

    async with job_sessionmaker(get_settings().database_url) as sessionmaker:
        outcome = await process.retry_file(sessionmaker, get_paths(), file_id)
    return outcome.value


@job_app.task(name="rehash", queue="maintenance")
async def rehash(missing_only: bool = False) -> dict[str, object]:
    from app.dedup import service

    async with job_sessionmaker(get_settings().database_url) as sessionmaker:
        stats = await service.rehash(sessionmaker, get_paths(), missing_only=missing_only)
    logger.info("rehash finished: %s", stats.as_dict())
    return stats.as_dict()


@job_app.task(name="recompute_similar", queue="maintenance")
async def recompute_similar() -> dict[str, int]:
    from app.dedup import service

    async with job_sessionmaker(get_settings().database_url) as sessionmaker:
        stats = await service.recompute_similar(sessionmaker)
    logger.info("recompute_similar finished: %s", stats.as_dict())
    return stats.as_dict()


# Daily at 03:17 UTC (12:17 in Asia/Seoul). The worker's periodic deferrer queues it.
@job_app.periodic(cron="17 3 * * *")
@job_app.task(name="purge_trash", queue="maintenance", queueing_lock="purge_trash")
async def purge_trash(timestamp: int | None = None) -> dict[str, int]:
    from app.review.trash import run_purge_trash

    settings = get_settings()
    stats = await run_purge_trash(settings.database_url, get_paths(), settings.trash_retention_days)
    logger.info("purge_trash finished: %s", stats.as_dict())
    return stats.as_dict()
