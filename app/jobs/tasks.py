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


async def _maintenance(kind: str, job_run_id: int | None, work: Any) -> str:
    """Run work(sessionmaker, settings, log) as the job run job_run_id (app.ops.runs)."""
    from app.db.models_crawl import JobKind
    from app.ops.runs import execute
    from app.ops.settings import load_settings

    paths = get_paths()
    async with job_sessionmaker(get_settings().database_url) as sessionmaker:
        async with sessionmaker() as session:
            settings = await load_settings(session)

        async def body(log: logging.Logger) -> dict[str, Any]:
            return await work(sessionmaker, settings, log)

        status = await execute(sessionmaker, paths, JobKind(kind), job_run_id, body)
    return status.value if status is not None else "skipped"


@job_app.task(name="rehash", queue="maintenance")
async def rehash(missing_only: bool = False, job_run_id: int | None = None) -> str:
    from app.dedup import service

    async def work(sessionmaker, settings, log) -> dict[str, Any]:
        stats = await service.rehash(sessionmaker, get_paths(), missing_only=missing_only)
        log.info("rehash finished: %s", stats.as_dict())
        return stats.as_dict()

    return await _maintenance("rehash", job_run_id, work)


@job_app.task(name="recompute_similar", queue="maintenance")
async def recompute_similar(job_run_id: int | None = None) -> str:
    from app.dedup import service

    async def work(sessionmaker, settings, log) -> dict[str, Any]:
        stats = await service.recompute_similar(sessionmaker)
        log.info("recompute_similar finished: %s", stats.as_dict())
        return stats.as_dict()

    return await _maintenance("recompute_similar", job_run_id, work)


@job_app.task(name="purge_trash", queue="maintenance")
async def purge_trash(job_run_id: int | None = None) -> str:
    from app.review.trash import purge_trash as purge

    async def work(sessionmaker, settings, log) -> dict[str, Any]:
        async with sessionmaker() as session:
            stats = await purge(session, get_paths(), settings.trash_retention_days)
        log.info("purge_trash finished: %s", stats.as_dict())
        return {**stats.as_dict(), "retention_days": settings.trash_retention_days}

    return await _maintenance("purge_trash", job_run_id, work)


@job_app.task(name="backup", queue="maintenance")
async def backup(job_run_id: int | None = None) -> str:
    from app.ops.backup import backup_database

    async def work(sessionmaker, settings, log) -> dict[str, Any]:
        result = await backup_database(
            get_settings().database_url, get_paths(), settings.backup_retention, log
        )
        return result.as_dict()

    return await _maintenance("backup", job_run_id, work)


@job_app.task(name="integrity", queue="maintenance")
async def integrity(job_run_id: int | None = None, verify_hashes: bool = False) -> str:
    from app.ops.integrity import check

    async def work(sessionmaker, settings, log) -> dict[str, Any]:
        report = await check(
            sessionmaker,
            get_paths(),
            retention_days=settings.trash_retention_days,
            verify_hashes=verify_hashes,
            log=log,
        )
        return report.as_dict()

    return await _maintenance("integrity", job_run_id, work)


@job_app.task(name="update_tools", queue="maintenance")
async def update_tools(job_run_id: int | None = None) -> str:
    from app.ops.tools_update import update_tools as update

    async def work(sessionmaker, settings, log) -> dict[str, Any]:
        return await update(get_paths(), log)

    return await _maintenance("update_tools", job_run_id, work)


@job_app.task(name="housekeeping", queue="maintenance")
async def housekeeping(job_run_id: int | None = None) -> str:
    from app.ops.housekeeping import housekeeping as run

    async def work(sessionmaker, settings, log) -> dict[str, Any]:
        return await run(sessionmaker, get_paths(), log)

    return await _maintenance("housekeeping", job_run_id, work)


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
    from app.ops.settings import load_settings

    async with (
        job_sessionmaker(get_settings().database_url) as sessionmaker,
        sessionmaker() as session,
    ):
        settings = await load_settings(session)
        result = await schedule_due_runs(session, settings.zone)
    return {
        "queued": result.queued,
        "skipped_active": result.skipped_active,
        "reaped": result.reaped,
    }


# Every minute: queue the maintenance runs whose schedule (APP_TIMEZONE) is due.
@job_app.periodic(cron="* * * * *")
@job_app.task(
    name="schedule_maintenance", queue="maintenance", queueing_lock="schedule_maintenance"
)
async def schedule_maintenance(timestamp: int | None = None) -> dict[str, object]:
    from app.ops.schedule import schedule_maintenance as tick
    from app.ops.settings import load_settings

    async with (
        job_sessionmaker(get_settings().database_url) as sessionmaker,
        sessionmaker() as session,
    ):
        settings = await load_settings(session)
        result = await tick(session, settings)
    return {"queued": result.queued, "skipped_active": result.skipped_active}


def _discover_addons() -> None:
    """Add-on discovery at worker startup (the worker imports this module when it starts)."""
    from app.crawl.registry import get_registry

    get_registry()


_discover_addons()
