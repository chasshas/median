"""run_crawl(job_run_id): run one queued crawl of a source (ARCHITECTURE.md section 11)."""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.crawl.base import CrawlContext, CrawledItem
from app.crawl.registry import Registry
from app.crawl.service import run_config
from app.db.models_crawl import CrawlSource, JobRun, JobStatus
from app.db.models_files import FileSource
from app.ingest.origin import IngestOrigin
from app.ingest.service import ingest
from app.storage import DataPaths
from app.storage.fs import fresh_dir, remove_tree

logger = logging.getLogger(__name__)

CANCEL_POLL_SECONDS = 1.0  # ctx.is_cancelled() sees a cancel request within 2 s
STOP_GRACE_SECONDS = 5.0
SECONDS_PER_MINUTE = 60.0  # time an add-on gets to return by itself after a cancel
MAX_ERROR_LENGTH = 2000
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


class RunFailure(Exception):
    pass


class RunBackend:
    """What CrawlContext delegates to: the ingest service and file_sources lookups."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        paths: DataPaths,
        source: CrawlSource,
        job_run_id: int,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.paths = paths
        self.source = source
        self.job_run_id = job_run_id

    async def emit(self, item: CrawledItem) -> None:
        origin = IngestOrigin(
            source_key=self.source.addon_key,
            remote_id=item.remote_id,
            url=item.url,
            source_id=self.source.id,
            job_run_id=self.job_run_id,
            tags=[*item.tags, *self.source.default_tags],
            tag_origin="crawler",
            title=item.title,
            description=item.description,
            metadata=item.metadata,
            default_folder_id=self.source.default_folder_id,
        )
        await ingest(
            item.path, origin, original_filename=item.filename or item.path.name, paths=self.paths
        )

    async def is_known(self, remote_id: str) -> bool:
        async with self.sessionmaker() as session:
            found = await session.scalar(
                select(FileSource.id)
                .where(
                    FileSource.source_key == self.source.addon_key,
                    FileSource.remote_id == remote_id,
                )
                .limit(1)
            )
        return found is not None

    async def known_remote_ids(self) -> set[str]:
        async with self.sessionmaker() as session:
            rows = await session.scalars(
                select(FileSource.remote_id).where(
                    FileSource.source_key == self.source.addon_key,
                    FileSource.remote_id.is_not(None),
                )
            )
            return {r for r in rows.all() if r is not None}


def open_run_log(paths: DataPaths, job_run_id: int) -> tuple[logging.Logger, logging.Handler]:
    """A logger writing to logs/jobs/<id>.log (and, through the root logger, to stdout)."""
    path = paths.job_log(job_run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    log = logging.getLogger(f"median.crawl.run.{job_run_id}")
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    return log, handler


def _message(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]


async def _claim(
    sessionmaker: async_sessionmaker[AsyncSession], job_run_id: int, log_path: str
) -> tuple[bool, CrawlSource | None]:
    """queued -> running, atomically (a concurrent cancel wins if it came first)."""
    async with sessionmaker() as session:
        row = (
            await session.execute(
                update(JobRun)
                .where(JobRun.id == job_run_id, JobRun.status == JobStatus.queued)
                .values(status=JobStatus.running, started_at=datetime.now(UTC), log_path=log_path)
                .returning(JobRun.source_id)
            )
        ).first()
        source = None
        if row is not None and row.source_id is not None:
            source = await session.get(CrawlSource, row.source_id)
        await session.commit()
    return row is not None, source


async def _watch_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_run_id: int,
    ctx: CrawlContext,
    event: asyncio.Event,
    log: logging.Logger,
) -> None:
    """Every second: store the emitted count (for the live page) and look for a cancel request."""
    while not event.is_set():
        await asyncio.sleep(CANCEL_POLL_SECONDS)
        async with sessionmaker() as session:
            requested = await session.scalar(
                update(JobRun)
                .where(JobRun.id == job_run_id)
                .values(stats={"emitted": ctx.emitted})
                .returning(JobRun.cancel_requested)
            )
            await session.commit()
        if requested is None or requested:
            log.warning("cancel requested")
            event.set()


async def _execute(
    addon_task: asyncio.Task[None],
    cancel_event: asyncio.Event,
    max_runtime: float,
    log: logging.Logger,
) -> tuple[JobStatus, str | None]:
    """Wait for the add-on, a cancel request or the runtime limit."""
    cancel_wait = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait(
            {addon_task, cancel_wait}, timeout=max_runtime, return_when=asyncio.FIRST_COMPLETED
        )
        if addon_task in done:
            exc = addon_task.exception()
            if exc is not None:
                log.error("add-on failed", exc_info=exc)
                return JobStatus.failed, _message(exc)
            if cancel_event.is_set():
                return JobStatus.cancelled, None
            return JobStatus.succeeded, None
        if cancel_wait in done:
            status, error = JobStatus.cancelled, None
            log.warning("stopping the add-on")
        else:
            status = JobStatus.failed
            error = f"exceeded the maximum runtime of {max_runtime / SECONDS_PER_MINUTE:g} minutes"
            log.error("%s; stopping the add-on", error)
            cancel_event.set()  # stops subprocesses; ctx.is_cancelled() turns true
        # Let the add-on return by itself (its subprocesses are already being stopped).
        await asyncio.wait({addon_task}, timeout=STOP_GRACE_SECONDS)
        if not addon_task.done():
            addon_task.cancel()
            await asyncio.wait({addon_task}, timeout=STOP_GRACE_SECONDS)
        if addon_task.done() and not addon_task.cancelled():
            late = addon_task.exception()
            if late is not None:
                log.info("add-on raised while stopping: %s", _message(late))
        return status, error
    finally:
        cancel_wait.cancel()
        if not addon_task.done():
            addon_task.cancel()


async def run_crawl(
    sessionmaker: async_sessionmaker[AsyncSession],
    paths: DataPaths,
    registry: Registry,
    job_run_id: int,
) -> JobStatus:
    """Run a queued crawl. Returns the final status (or the current one if not queued)."""
    log_file = paths.job_log(job_run_id)
    log_path = log_file.relative_to(paths.root).as_posix()
    claimed, source = await _claim(sessionmaker, job_run_id, log_path)
    if not claimed:
        async with sessionmaker() as session:
            current = await session.scalar(select(JobRun.status).where(JobRun.id == job_run_id))
        logger.info("run %s not started: it is %s", job_run_id, current)
        return current or JobStatus.failed

    log, handler = open_run_log(paths, job_run_id)
    work_dir = paths.work_dir(job_run_id)
    status, error = JobStatus.failed, None
    ctx: CrawlContext | None = None
    try:
        if source is None:
            raise RunFailure("the source was deleted")
        info = registry.get(source.addon_key)
        if info is None:
            raise RunFailure(f"add-on {source.addon_key!r} is not loaded")
        log.info(
            "run %s of source %s (%s) with add-on %s %s",
            job_run_id,
            source.id,
            source.name,
            info.key,
            info.version,
        )
        try:
            config = run_config(info, source, paths)
        except Exception as exc:
            raise RunFailure(f"invalid configuration: {exc}") from exc
        fresh_dir(work_dir)
        state_dir = paths.addon_state_dir(source.id)
        state_dir.mkdir(parents=True, exist_ok=True)
        cancel_event = asyncio.Event()
        ctx = CrawlContext(
            work_dir=work_dir,
            state_dir=state_dir,
            secrets_dir=paths.secrets_dir(source.id),
            log=log,
            backend=RunBackend(sessionmaker, paths, source, job_run_id),
            cancel_event=cancel_event,
            source_id=source.id,
            job_run_id=job_run_id,
        )
        watcher = asyncio.create_task(_watch_run(sessionmaker, job_run_id, ctx, cancel_event, log))
        try:
            status, error = await _execute(
                asyncio.create_task(info.addon.run(ctx, config)),
                cancel_event,
                source.max_runtime_minutes * SECONDS_PER_MINUTE,
                log,
            )
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
    except RunFailure as exc:
        error = str(exc)
        log.error("%s", error)
    except Exception as exc:
        error = _message(exc)
        log.exception("run failed")
    finally:
        remove_tree(work_dir)
        emitted = ctx.emitted if ctx is not None else 0
        async with sessionmaker() as session:
            await session.execute(
                update(JobRun)
                .where(JobRun.id == job_run_id)
                .values(
                    status=status,
                    finished_at=datetime.now(UTC),
                    error=error,
                    stats={"emitted": emitted},
                )
            )
            await session.commit()
        log.info("run %s %s (emitted %d)", job_run_id, status.value, emitted)
        log.removeHandler(handler)
        handler.close()
    return status
