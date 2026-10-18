"""Daily housekeeping and worker-startup recovery.

housekeeping: delete job runs and job logs older than 90 days (ingest_events stay forever),
re-queue files stuck in processing, and measure disk usage per storage area.
recover_on_startup: before the worker starts, runs and queue jobs left running by a stopped
worker are marked failed ("interrupted"), and files stuck in processing are re-queued.
The worker assumes it is the only one (one worker service in docker-compose.yml).
"""

import asyncio
import contextlib
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models_crawl import ACTIVE_STATUSES, JobRun, JobStatus
from app.db.models_files import File, FileStatus
from app.jobs import defer_retry_file, delete_old_queue_jobs, fail_queue_job, stale_running_jobs
from app.ops.runs import INTERRUPTED
from app.storage import DataPaths

logger = logging.getLogger(__name__)

RETENTION_DAYS = 90
STUCK_AFTER = timedelta(hours=1)
LOW_SPACE_RATIO = 0.10
# Storage areas of ARCHITECTURE.md section 5; everything else under DATA_DIR is "other".
AREAS = ("staging", "archive", "thumbs", "trash", "work", "backups", "logs", "tools")


# Stuck files -----------------------------------------------------------------------------------


async def live_ingest_targets(session: AsyncSession) -> tuple[set[int], set[str]]:
    """File ids of waiting or running retry_file jobs, staging paths of process_ingest jobs."""
    exists = await session.scalar(text("SELECT to_regclass('public.procrastinate_jobs')"))
    if exists is None:
        return set(), set()
    rows = (
        await session.execute(
            text(
                "SELECT task_name, args FROM procrastinate_jobs"
                " WHERE task_name IN ('retry_file', 'process_ingest')"
                " AND status IN ('todo', 'doing')"
            )
        )
    ).all()
    ids, staged = set(), set()
    for task_name, args in rows:
        if task_name == "retry_file" and args.get("file_id") is not None:
            ids.add(int(args["file_id"]))
        elif args.get("staging_path"):
            staged.add(str(args["staging_path"]))
    return ids, staged


async def requeue_stuck_files(
    sessionmaker: async_sessionmaker[AsyncSession], now: datetime | None = None
) -> list[int]:
    """Defer retry_file for files in processing for more than an hour without a live job."""
    cutoff = (now or datetime.now(UTC)) - STUCK_AFTER
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(File.id, File.staging_path).where(
                    File.status == FileStatus.processing, File.collected_at < cutoff
                )
            )
        ).all()
        live_ids, live_paths = await live_ingest_targets(session)
    requeued = []
    for file_id, staging_path in rows:
        if file_id in live_ids or (staging_path and staging_path in live_paths):
            continue
        await defer_retry_file(file_id)
        requeued.append(file_id)
    if requeued:
        logger.warning("re-queued files stuck in processing: %s", requeued)
    return requeued


# Startup recovery ------------------------------------------------------------------------------


@dataclass
class Recovery:
    interrupted_runs: list[int]
    failed_queue_jobs: list[int]
    requeued_files: list[int]

    def as_dict(self) -> dict[str, list[int]]:
        return {
            "interrupted_runs": self.interrupted_runs,
            "failed_queue_jobs": self.failed_queue_jobs,
            "requeued_files": self.requeued_files,
        }


async def recover_on_startup(
    sessionmaker: async_sessionmaker[AsyncSession], now: datetime | None = None
) -> Recovery:
    """Clean up after a worker that stopped mid-job. Call before the worker starts (the job
    queue must be open)."""
    now = now or datetime.now(UTC)
    async with sessionmaker() as session:
        stale = await stale_running_jobs(session)
    failed = [job_id for job_id in stale if await fail_queue_job(job_id)]
    async with sessionmaker() as session:
        interrupted = list(
            (
                await session.scalars(
                    update(JobRun)
                    .where(JobRun.status == JobStatus.running)
                    .values(status=JobStatus.failed, error=INTERRUPTED, finished_at=now)
                    .returning(JobRun.id)
                )
            ).all()
        )
        await session.commit()
    if interrupted:
        logger.warning("marked runs interrupted by the last worker stop: %s", interrupted)
    if failed:
        logger.warning("marked queue jobs left running as failed: %s", failed)
    requeued = await requeue_stuck_files(sessionmaker, now)
    return Recovery(sorted(interrupted), failed, requeued)


# Retention -------------------------------------------------------------------------------------


async def delete_old_runs(
    sessionmaker: async_sessionmaker[AsyncSession],
    paths: DataPaths,
    now: datetime | None = None,
    days: int = RETENTION_DAYS,
) -> tuple[int, int]:
    """Delete finished job runs (and their logs) older than `days`, and stray logs. Returns
    (runs deleted, log files deleted). ingest_events keep their rows (job_run_id SET NULL)."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                delete(JobRun)
                .where(
                    JobRun.status.not_in(ACTIVE_STATUSES),
                    JobRun.queued_at < cutoff,
                )
                .returning(JobRun.id, JobRun.log_path)
            )
        ).all()
        kept = set((await session.scalars(select(JobRun.id))).all())
        await session.commit()
    logs = 0
    for run_id, log_path in rows:
        target = paths.job_log(run_id)
        if log_path:
            with contextlib.suppress(ValueError):
                target = paths.resolve(log_path)
        if target.is_file():
            target.unlink(missing_ok=True)
            logs += 1
    if paths.job_logs.is_dir():
        limit = cutoff.timestamp()
        for path in paths.job_logs.iterdir():
            stem = path.stem
            if stem.isdigit() and int(stem) in kept:
                continue
            if path.is_file() and path.stat().st_mtime < limit:
                path.unlink(missing_ok=True)
                logs += 1
    await delete_old_queue_jobs(days * 24)
    return len(rows), logs


# Disk usage ------------------------------------------------------------------------------------


def tree_size(path: Path) -> int:
    """Apparent size of every regular file below path (symlinks are not followed)."""
    total = 0
    if not path.exists():
        return 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def free_space(paths: DataPaths) -> dict[str, Any]:
    """Size and free space of the filesystem holding DATA_DIR (cheap; read live)."""
    target = paths.root if paths.root.exists() else paths.root.parent
    usage = shutil.disk_usage(target)
    ratio = usage.free / usage.total if usage.total else 1.0
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "free_ratio": ratio,
        "low": ratio < LOW_SPACE_RATIO,
    }


def disk_usage(paths: DataPaths, now: datetime | None = None) -> dict[str, Any]:
    areas = {name: tree_size(paths.root / name) for name in AREAS}
    areas["other"] = max(tree_size(paths.root) - sum(areas.values()), 0)
    return {
        "areas": areas,
        **free_space(paths),
        "measured_at": (now or datetime.now(UTC)).isoformat(),
    }


async def housekeeping(
    sessionmaker: async_sessionmaker[AsyncSession], paths: DataPaths, log: logging.Logger
) -> dict[str, Any]:
    runs, logs = await delete_old_runs(sessionmaker, paths)
    log.info("deleted %d job runs and %d job logs older than %d days", runs, logs, RETENTION_DAYS)
    requeued = await requeue_stuck_files(sessionmaker)
    if requeued:
        log.warning("re-queued files stuck in processing: %s", requeued)
    disk = await asyncio.to_thread(disk_usage, paths)
    log.info("disk usage: %s", disk)
    return {
        "deleted_runs": runs,
        "deleted_logs": logs,
        "requeued_files": requeued,
        "disk": disk,
    }
