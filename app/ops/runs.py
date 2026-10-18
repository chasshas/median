"""Maintenance runs (backup, integrity, update_tools, purge_trash, rehash, recompute_similar,
housekeeping) recorded in job_runs like crawls: queued -> running -> succeeded or failed.

queue_maintenance() is what /settings and the scheduler call; execute() is what the task
bodies call. At most one run per kind is queued or running (uq_job_runs_active_kind).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.crawl.runner import open_run_log
from app.db.models_crawl import ACTIVE_STATUSES, JobKind, JobRun, JobStatus
from app.jobs import AlreadyQueued, defer_maintenance
from app.storage import DataPaths

logger = logging.getLogger(__name__)

MAINTENANCE_KINDS = (
    JobKind.backup,
    JobKind.integrity,
    JobKind.update_tools,
    JobKind.housekeeping,
    JobKind.purge_trash,
    JobKind.rehash,
    JobKind.recompute_similar,
)
MAX_ERROR_LENGTH = 2000
INTERRUPTED = "interrupted"

Body = Callable[[logging.Logger], Awaitable[dict[str, Any]]]


class MaintenanceActive(Exception):
    def __init__(self, kind: JobKind, run_id: int | None) -> None:
        super().__init__(f"a {kind.value} run is already queued or running")
        self.kind = kind
        self.run_id = run_id


class JobFailed(Exception):
    """Raised by a body to fail the run with a message while keeping its stats."""

    def __init__(self, message: str, stats: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.stats = stats or {}


async def active_run(session: AsyncSession, kind: JobKind) -> int | None:
    return await session.scalar(
        select(JobRun.id).where(JobRun.kind == kind, JobRun.status.in_(ACTIVE_STATUSES)).limit(1)
    )


async def queue_maintenance(
    session: AsyncSession, kind: JobKind, params: dict[str, Any] | None = None
) -> JobRun:
    """Insert a queued run and defer its task. Commits. MaintenanceActive if one is active."""
    if kind not in MAINTENANCE_KINDS:
        raise ValueError(f"{kind.value} is not a maintenance job")
    params = params or {}
    existing = await active_run(session, kind)
    if existing is not None:
        await session.rollback()
        raise MaintenanceActive(kind, existing)
    run = JobRun(kind=kind, status=JobStatus.queued, stats={"options": params} if params else {})
    session.add(run)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise MaintenanceActive(kind, None) from exc
    await session.commit()
    try:
        run.queue_job_id = await defer_maintenance(kind.value, run.id, params)
    except AlreadyQueued as exc:
        await session.delete(run)
        await session.commit()
        raise MaintenanceActive(kind, None) from exc
    except Exception as exc:
        run.status = JobStatus.failed
        run.error = f"could not queue the run: {exc}"[:MAX_ERROR_LENGTH]
        run.finished_at = datetime.now(UTC)
        await session.commit()
        raise
    await session.commit()
    return run


async def _start(
    sessionmaker: async_sessionmaker[AsyncSession],
    paths: DataPaths,
    kind: JobKind,
    job_run_id: int | None,
) -> int | None:
    """Claim the queued run (queued -> running), or insert a running one when the task was
    deferred without a run. None when there is nothing to run."""
    now = datetime.now(UTC)
    async with sessionmaker() as session:
        if job_run_id is None:
            run = JobRun(kind=kind, status=JobStatus.running, started_at=now)
            session.add(run)
            try:
                await session.flush()
            except IntegrityError:
                logger.info("%s not started: another run is active", kind.value)
                return None
            run.log_path = paths.job_log(run.id).relative_to(paths.root).as_posix()
            await session.commit()
            return run.id
        claimed = await session.scalar(
            update(JobRun)
            .where(JobRun.id == job_run_id, JobRun.status == JobStatus.queued)
            .values(
                status=JobStatus.running,
                started_at=now,
                log_path=paths.job_log(job_run_id).relative_to(paths.root).as_posix(),
            )
            .returning(JobRun.id)
        )
        await session.commit()
    if claimed is None:
        logger.info("%s run %s not started: it is no longer queued", kind.value, job_run_id)
    return claimed


async def execute(
    sessionmaker: async_sessionmaker[AsyncSession],
    paths: DataPaths,
    kind: JobKind,
    job_run_id: int | None,
    body: Body,
) -> JobStatus | None:
    """Run body(log) as a job run and store its stats. Returns the final status, or None
    when the run was not started (cancelled, or another run of the kind is active)."""
    paths.root.mkdir(parents=True, exist_ok=True)
    run_id = await _start(sessionmaker, paths, kind, job_run_id)
    if run_id is None:
        return None
    log, handler = open_run_log(paths, run_id)
    status, error, stats = JobStatus.failed, None, {}
    async with sessionmaker() as session:
        previous = await session.scalar(select(JobRun.stats).where(JobRun.id == run_id))
    options = (previous or {}).get("options")
    try:
        log.info("%s run %s started", kind.value, run_id)
        stats = await body(log)
        status = JobStatus.succeeded
    except JobFailed as exc:
        error, stats = str(exc), exc.stats
        log.error("%s", error)
    except asyncio.CancelledError:
        error = INTERRUPTED
        log.error("interrupted")
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log.exception("%s run failed", kind.value)
    finally:
        if options:
            stats = {"options": options, **stats}
        async with sessionmaker() as session:
            await session.execute(
                update(JobRun)
                .where(JobRun.id == run_id)
                .values(
                    status=status,
                    finished_at=datetime.now(UTC),
                    error=error[:MAX_ERROR_LENGTH] if error else None,
                    stats=stats,
                )
            )
            await session.commit()
        log.info("%s run %s %s", kind.value, run_id, status.value)
        log.removeHandler(handler)
        handler.close()
    return status


async def latest_run(
    session: AsyncSession, kind: JobKind, status: JobStatus | None = None
) -> JobRun | None:
    stmt = select(JobRun).where(JobRun.kind == kind)
    if status is not None:
        stmt = stmt.where(JobRun.status == status)
    return await session.scalar(stmt.order_by(JobRun.id.desc()).limit(1))
