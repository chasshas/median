"""Background jobs facade. The rest of the app uses these functions, never Procrastinate."""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.app import job_app, make_connector


@contextlib.asynccontextmanager
async def open_jobs(database_url: str) -> AsyncIterator[None]:
    """Open the job queue on database_url for the duration of the block (web process, CLI)."""
    with job_app.replace_connector(make_connector(database_url)):
        async with job_app.open_async():
            yield


async def run_queued_jobs() -> None:
    """Run every queued job in this process, then return (tests and one-off commands)."""
    await job_app.run_worker_async(wait=False, install_signal_handlers=False)


async def defer_ping(message: str = "ping") -> int:
    """Queue the demo ping task and return the job id."""
    from app.jobs.tasks import ping

    return await ping.defer_async(message=message)


async def defer_process_ingest(
    *, staging_path: str, origin: dict[str, Any], original_filename: str, batch_id: str
) -> int:
    from app.jobs.tasks import process_ingest

    return await process_ingest.defer_async(
        staging_path=staging_path,
        origin=origin,
        original_filename=original_filename,
        batch_id=batch_id,
    )


async def defer_retry_file(file_id: int) -> int:
    from app.jobs.tasks import retry_file

    return await retry_file.defer_async(file_id=file_id)


async def defer_rehash(*, missing_only: bool = False) -> int:
    from app.jobs.tasks import rehash

    return await rehash.defer_async(missing_only=missing_only)


async def defer_recompute_similar() -> int:
    from app.jobs.tasks import recompute_similar

    return await recompute_similar.defer_async()


async def defer_purge_trash() -> int:
    from app.jobs.tasks import purge_trash

    return await purge_trash.defer_async()


async def job_counts(session: AsyncSession) -> dict[str, int]:
    """Number of jobs per status (todo, doing, succeeded, failed, ...); empty before migrate."""
    exists = await session.scalar(text("SELECT to_regclass('public.procrastinate_jobs')"))
    if exists is None:
        return {}
    result = await session.execute(
        text("SELECT status::text, count(*) FROM procrastinate_jobs GROUP BY status")
    )
    return {status: count for status, count in result.all()}


async def pending_ingest_jobs(session: AsyncSession, batch_id: str) -> int:
    """Queued or running process_ingest jobs of one staging batch."""
    exists = await session.scalar(text("SELECT to_regclass('public.procrastinate_jobs')"))
    if exists is None:
        return 0
    count = await session.scalar(
        text(
            "SELECT count(*) FROM procrastinate_jobs WHERE task_name = 'process_ingest'"
            " AND status IN ('todo', 'doing') AND args->>'batch_id' = :batch_id"
        ),
        {"batch_id": batch_id},
    )
    return int(count or 0)
