"""Background jobs facade. The rest of the app uses these functions, never Procrastinate."""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.app import job_app, make_connector


class AlreadyQueued(Exception):
    """A job with the same queueing lock is already waiting in the queue."""


@contextlib.asynccontextmanager
async def open_jobs(database_url: str) -> AsyncIterator[None]:
    """Open the job queue on database_url for the duration of the block (web process, CLI)."""
    with job_app.replace_connector(make_connector(database_url)):
        async with job_app.open_async():
            yield


async def run_queued_jobs() -> None:
    """Run every queued job in this process, then return (tests and one-off commands).

    Periodic tasks are not deferred here: only the long-running worker schedules them.
    """
    from procrastinate.periodic import PeriodicRegistry

    registry = job_app.periodic_registry
    job_app.periodic_registry = PeriodicRegistry()
    try:
        await job_app.run_worker_async(wait=False, install_signal_handlers=False)
    finally:
        job_app.periodic_registry = registry


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


MAINTENANCE_TASKS = {
    "backup": "backup",
    "integrity": "integrity",
    "update_tools": "update_tools",
    "housekeeping": "housekeeping",
    "purge_trash": "purge_trash",
    "rehash": "rehash",
    "recompute_similar": "recompute_similar",
}


async def defer_maintenance(kind: str, job_run_id: int, params: dict[str, Any]) -> int:
    """Queue the task of a maintenance run (job_runs row job_run_id). One waiting job per kind."""
    import procrastinate.exceptions

    from app.jobs import tasks

    task = getattr(tasks, MAINTENANCE_TASKS[kind])
    try:
        return await task.configure(queueing_lock=f"maintenance-{kind}").defer_async(
            job_run_id=job_run_id, **params
        )
    except procrastinate.exceptions.AlreadyEnqueued as exc:
        raise AlreadyQueued(str(exc)) from exc


async def stale_running_jobs(session: AsyncSession) -> list[int]:
    """Queue jobs left in status doing (their worker is gone when a new worker starts)."""
    exists = await session.scalar(text("SELECT to_regclass('public.procrastinate_jobs')"))
    if exists is None:
        return []
    rows = await session.scalars(text("SELECT id FROM procrastinate_jobs WHERE status = 'doing'"))
    return [int(i) for i in rows.all()]


async def fail_queue_job(job_id: int) -> bool:
    """Mark a queue job that is stuck in doing as failed (frees its locks)."""
    from procrastinate.jobs import Status

    try:
        await job_app.job_manager.finish_job_by_id_async(
            job_id=job_id, status=Status.FAILED, delete_job=False
        )
    except Exception:
        return False
    return True


async def delete_old_queue_jobs(hours: int) -> None:
    """Delete queue jobs that finished more than `hours` ago, whatever their final status."""
    await job_app.job_manager.delete_old_jobs(
        nb_hours=hours, include_failed=True, include_cancelled=True, include_aborted=True
    )


def crawl_lock(source_id: int) -> str:
    return f"crawl-source-{source_id}"


async def defer_run_crawl(job_run_id: int, source_id: int | None) -> int:
    """Queue run_crawl. Per source: one waiting job (queueing lock), one running job (lock)."""
    import procrastinate.exceptions

    from app.jobs.tasks import run_crawl

    options: dict[str, Any] = {}
    if source_id is not None:
        options = {"queueing_lock": crawl_lock(source_id), "lock": crawl_lock(source_id)}
    try:
        return await run_crawl.configure(**options).defer_async(job_run_id=job_run_id)
    except procrastinate.exceptions.AlreadyEnqueued as exc:
        raise AlreadyQueued(str(exc)) from exc


async def cancel_queue_job(job_id: int) -> bool:
    """Cancel a queue job that has not started yet. True if it was cancelled."""
    try:
        return await job_app.job_manager.cancel_job_by_id_async(job_id)
    except Exception:
        return False


async def queue_job_statuses(session: AsyncSession, job_ids: list[int]) -> dict[int, str]:
    """Status (todo, doing, succeeded, failed, cancelled, aborted) of queue jobs by id."""
    if not job_ids:
        return {}
    result = await session.execute(
        text("SELECT id, status::text FROM procrastinate_jobs WHERE id = ANY(:ids)"),
        {"ids": job_ids},
    )
    return {int(job_id): status for job_id, status in result.all()}


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
