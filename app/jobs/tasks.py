"""Task registration. Each task delegates to a service; keep task bodies thin."""

import logging
from typing import Any
from zoneinfo import ZoneInfo

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


@job_app.task(name="run_crawl", queue="crawl")
async def run_crawl(job_run_id: int) -> str:
    from app.crawl import runner
    from app.crawl.registry import get_registry

    async with job_sessionmaker(get_settings().database_url) as sessionmaker:
        status = await runner.run_crawl(sessionmaker, get_paths(), get_registry(), job_run_id)
    return status.value


# Every minute: queue the runs of sources whose cron schedule is due.
@job_app.periodic(cron="* * * * *")
@job_app.task(name="schedule_crawls", queue="maintenance", queueing_lock="schedule_crawls")
async def schedule_crawls(timestamp: int | None = None) -> dict[str, object]:
    from app.crawl.scheduling import schedule_due_runs

    settings = get_settings()
    async with (
        job_sessionmaker(settings.database_url) as sessionmaker,
        sessionmaker() as session,
    ):
        result = await schedule_due_runs(session, ZoneInfo(settings.app_timezone))
    return {
        "queued": result.queued,
        "skipped_active": result.skipped_active,
        "reaped": result.reaped,
    }


def _discover_addons() -> None:
    """Add-on discovery at worker startup (the worker imports this module when it starts)."""
    from app.crawl.registry import get_registry

    get_registry()


_discover_addons()
