"""Crawl sources and job runs: create, edit, queue, cancel, stats.

Services own their transactions: functions that queue or cancel commit themselves.
"""

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from croniter import croniter
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawl.base import secret_fields
from app.crawl.registry import AddonInfo, Registry
from app.db.models_crawl import ACTIVE_STATUSES, CrawlSource, JobKind, JobRun, JobStatus
from app.db.models_files import IngestEvent, IngestOutcome
from app.jobs import AlreadyQueued, cancel_queue_job, defer_run_crawl, queue_job_statuses
from app.library.folders import folder_exists
from app.library.tags import normalize_tags
from app.storage import DataPaths, run_after_commit
from app.storage.fs import remove_tree, unlink_quietly, write_private_file

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 200
MAX_RUNTIME_LIMIT = 7 * 24 * 60
SECRET_PLACEHOLDER = Path("/secret-file")
# Queue job states in which a worker can still pick up or finish a run.
LIVE_QUEUE_STATES = ("todo", "doing")


class SourceError(ValueError):
    def __init__(self, message: str, fields: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.fields = fields or {}


class SourceNotFound(LookupError):
    pass


class RunNotFound(LookupError):
    pass


class RunActive(Exception):
    """The source already has a queued or running run."""

    def __init__(self, run_id: int | None) -> None:
        super().__init__(
            f"run {run_id} of this source is still queued or running"
            if run_id
            else "a run of this source is already queued"
        )
        self.run_id = run_id


class RunNotActive(Exception):
    pass


@dataclass
class SourceInput:
    name: str
    addon_key: str
    config: dict[str, Any] = field(default_factory=dict)
    schedule: str | None = None
    enabled: bool = True
    default_folder_id: int | None = None
    default_tags: list[str] = field(default_factory=list)
    max_runtime_minutes: int = 120


# Secret files ---------------------------------------------------------------------------


def secret_path(paths: DataPaths, source_id: int, name: str) -> Path:
    return paths.secrets_dir(source_id) / name


def present_secrets(paths: DataPaths, source_id: int | None, names: list[str]) -> set[str]:
    if source_id is None:
        return set()
    return {n for n in names if secret_path(paths, source_id, n).is_file()}


def run_config(info: AddonInfo, source: CrawlSource, paths: DataPaths) -> Any:
    """The validated config instance an add-on run receives (secrets as paths or None)."""
    names = secret_fields(info.config_model)
    values = {k: v for k, v in source.config.items() if k not in names}
    for name in names:
        path = secret_path(paths, source.id, name)
        values[name] = path if path.is_file() else None
    return info.config_model.model_validate(values)


# Validation -----------------------------------------------------------------------------


def validate_schedule(schedule: str | None) -> str | None:
    if schedule is None or not schedule.strip():
        return None
    value = " ".join(schedule.split())
    if len(value.split()) != 5 or not croniter.is_valid(value):
        raise SourceError(
            "invalid cron schedule (five fields, for example */30 * * * *)",
            {"schedule": "invalid cron expression"},
        )
    return value


def validation_messages(exc: ValidationError) -> dict[str, str]:
    out: dict[str, str] = {}
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "config"
        out.setdefault(loc, err["msg"])
    return out


def validate_config(info: AddonInfo, config: dict[str, Any], secrets: set[str]) -> dict[str, Any]:
    """Validate config against the add-on's model; returns the JSON to store (no secrets)."""
    model = info.config_model
    names = secret_fields(model)
    values = {k: v for k, v in config.items() if k not in names}
    for name in names:
        values[name] = SECRET_PLACEHOLDER if name in secrets else None
    try:
        instance = model.model_validate(values)
    except ValidationError as exc:
        raise SourceError("invalid add-on configuration", validation_messages(exc)) from exc
    return instance.model_dump(mode="json", exclude=set(names))


async def _check(session: AsyncSession, data: SourceInput) -> SourceInput:
    name = " ".join(data.name.split())
    if not name or len(name) > MAX_NAME_LENGTH:
        raise SourceError("name must be 1-200 characters", {"name": "required"})
    if not 1 <= data.max_runtime_minutes <= MAX_RUNTIME_LIMIT:
        raise SourceError(
            f"max runtime must be 1-{MAX_RUNTIME_LIMIT} minutes",
            {"max_runtime_minutes": "out of range"},
        )
    if data.default_folder_id is not None and not await folder_exists(
        session, data.default_folder_id
    ):
        raise SourceError("unknown default folder", {"default_folder_id": "unknown folder"})
    data.name = name
    data.schedule = validate_schedule(data.schedule)
    data.default_tags = normalize_tags(data.default_tags)
    return data


def _write_secrets(
    session: AsyncSession, paths: DataPaths, source_id: int, secrets: dict[str, bytes | None]
) -> None:
    for name, content in secrets.items():
        path = secret_path(paths, source_id, name)
        if content is None:
            run_after_commit(session, lambda p=path: unlink_quietly(p))
        else:
            run_after_commit(session, lambda p=path, c=content: write_private_file(p, c))


# Sources ---------------------------------------------------------------------------------


async def get_source(session: AsyncSession, source_id: int) -> CrawlSource:
    source = await session.get(CrawlSource, source_id)
    if source is None:
        raise SourceNotFound(source_id)
    return source


async def list_sources(session: AsyncSession) -> list[CrawlSource]:
    rows = await session.scalars(select(CrawlSource).order_by(func.lower(CrawlSource.name)))
    return list(rows.all())


async def create_source(
    session: AsyncSession,
    registry: Registry,
    paths: DataPaths,
    data: SourceInput,
    secrets: dict[str, bytes | None] | None = None,
) -> CrawlSource:
    """Create a source; secrets maps secret field names to uploaded bytes. Commits."""
    info = registry.get(data.addon_key)
    if info is None:
        raise SourceError(f"unknown add-on {data.addon_key!r}", {"addon_key": "unknown add-on"})
    data = await _check(session, data)
    uploads = {k: v for k, v in (secrets or {}).items() if v is not None}
    _known_secret_names(info, uploads)
    config = validate_config(info, data.config, set(uploads))
    source = CrawlSource(
        name=data.name,
        addon_key=data.addon_key,
        config=config,
        schedule=data.schedule,
        enabled=data.enabled,
        default_folder_id=data.default_folder_id,
        default_tags=data.default_tags,
        max_runtime_minutes=data.max_runtime_minutes,
    )
    session.add(source)
    await session.flush()
    _write_secrets(session, paths, source.id, uploads)
    await session.commit()
    return source


def _known_secret_names(info: AddonInfo, secrets: dict[str, bytes | None]) -> None:
    unknown = set(secrets) - set(secret_fields(info.config_model))
    if unknown:
        raise SourceError(f"not secret fields: {', '.join(sorted(unknown))}")


async def update_source(
    session: AsyncSession,
    registry: Registry,
    paths: DataPaths,
    source_id: int,
    data: SourceInput,
    secrets: dict[str, bytes | None] | None = None,
) -> CrawlSource:
    """Replace a source's settings. secrets: bytes = new file, None = remove, absent = keep."""
    source = await get_source(session, source_id)
    data.addon_key = source.addon_key  # the add-on of a source never changes
    data = await _check(session, data)
    secrets = secrets or {}
    info = registry.get(source.addon_key)
    if info is not None:
        _known_secret_names(info, secrets)
        names = secret_fields(info.config_model)
        present = present_secrets(paths, source.id, names)
        present |= {k for k, v in secrets.items() if v is not None}
        present -= {k for k, v in secrets.items() if v is None}
        source.config = validate_config(info, data.config, present)
    elif data.config != source.config:
        raise SourceError(f"add-on {source.addon_key!r} is not loaded; its config cannot change")
    source.name = data.name
    source.schedule = data.schedule
    source.enabled = data.enabled
    source.default_folder_id = data.default_folder_id
    source.default_tags = data.default_tags
    source.max_runtime_minutes = data.max_runtime_minutes
    _write_secrets(session, paths, source.id, secrets)
    await session.commit()
    return source


async def set_enabled(session: AsyncSession, source_id: int, enabled: bool) -> CrawlSource:
    source = await get_source(session, source_id)
    source.enabled = enabled
    await session.commit()
    return source


async def delete_source(session: AsyncSession, paths: DataPaths, source_id: int) -> None:
    """Delete a source without an active run, with its secrets and add-on state.

    Its files, sources rows and past runs stay (their source_id becomes NULL).
    """
    source = await session.get(CrawlSource, source_id, with_for_update=True)
    if source is None:
        raise SourceNotFound(source_id)
    active = await active_run_id(session, source_id)
    if active is not None:
        raise RunActive(active)
    await session.delete(source)
    run_after_commit(session, lambda: remove_tree(paths.secrets_dir(source_id)))
    run_after_commit(session, lambda: remove_tree(paths.addon_state_dir(source_id)))
    await session.commit()


# Runs ------------------------------------------------------------------------------------


async def active_run_id(session: AsyncSession, source_id: int) -> int | None:
    return await session.scalar(
        select(JobRun.id)
        .where(JobRun.source_id == source_id, JobRun.status.in_(ACTIVE_STATUSES))
        .limit(1)
    )


async def queue_run(session: AsyncSession, source_id: int) -> JobRun:
    """Queue a crawl run of a source now, unless one is queued or running. Commits."""
    source = await session.get(CrawlSource, source_id, with_for_update=True)
    if source is None:
        raise SourceNotFound(source_id)
    active = await active_run_id(session, source_id)
    if active is not None:
        await session.rollback()
        raise RunActive(active)
    run = JobRun(kind=JobKind.crawl, status=JobStatus.queued, source_id=source_id)
    session.add(run)
    source.last_run_at = datetime.now(UTC)
    try:
        await session.flush()
    except IntegrityError as exc:  # uq_job_runs_active_source: another request was faster
        await session.rollback()
        raise RunActive(None) from exc
    await session.commit()
    try:
        run.queue_job_id = await defer_run_crawl(run.id, source_id)
    except AlreadyQueued as exc:
        await session.delete(run)
        await session.commit()
        raise RunActive(None) from exc
    except Exception as exc:
        run.status = JobStatus.failed
        run.error = f"could not queue the run: {exc}"
        run.finished_at = datetime.now(UTC)
        await session.commit()
        raise
    await session.commit()
    return run


async def get_run(session: AsyncSession, run_id: int) -> JobRun:
    run = await session.get(JobRun, run_id)
    if run is None:
        raise RunNotFound(run_id)
    return run


async def cancel_run(session: AsyncSession, run_id: int) -> JobRun:
    """Cancel a queued run at once; ask a running one to stop (the runner sees it within 2 s).

    A run whose worker is gone (its queue job is no longer waiting or running) is marked
    cancelled directly. Commits.
    """
    run = await session.get(JobRun, run_id, with_for_update=True)
    if run is None:
        raise RunNotFound(run_id)
    if not run.is_active:
        message = f"run {run_id} is {run.status.value}"
        await session.rollback()
        raise RunNotActive(message)
    run.cancel_requested = True
    lost = False
    if run.status is JobStatus.running and run.queue_job_id is not None:
        states = await queue_job_statuses(session, [run.queue_job_id])
        lost = states.get(run.queue_job_id) not in LIVE_QUEUE_STATES
    queued = run.status is JobStatus.queued
    if queued or lost:
        run.status = JobStatus.cancelled
        run.finished_at = datetime.now(UTC)
        if lost:
            run.error = "cancelled after its worker stopped"
    job_id = run.queue_job_id
    await session.commit()
    if queued and job_id is not None:
        await cancel_queue_job(job_id)  # frees the queueing lock of the source
    return run


async def reap_lost_runs(session: AsyncSession) -> list[int]:
    """Mark runs failed whose queue job ended without them (for example a killed worker)."""
    rows = list(
        (
            await session.scalars(
                select(JobRun).where(
                    JobRun.status.in_(ACTIVE_STATUSES), JobRun.queue_job_id.is_not(None)
                )
            )
        ).all()
    )
    states = await queue_job_statuses(session, [r.queue_job_id for r in rows if r.queue_job_id])
    lost = [r for r in rows if states.get(r.queue_job_id or 0) not in LIVE_QUEUE_STATES]
    now = datetime.now(UTC)
    for run in lost:
        run.status = JobStatus.failed
        run.finished_at = now
        run.error = "the worker stopped before the run finished"
    await session.commit()
    return [r.id for r in lost]


# Stats -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunStats:
    emitted: int = 0
    new: int = 0
    forced_reingest: int = 0
    duplicate_exact: int = 0
    skipped_known_remote: int = 0
    errors: int = 0

    @property
    def processed(self) -> int:
        return (
            self.new
            + self.forced_reingest
            + self.duplicate_exact
            + self.skipped_known_remote
            + self.errors
        )

    @property
    def pending(self) -> int:
        """Emitted files whose ingest has not finished yet."""
        return max(self.emitted - self.processed, 0)

    def as_dict(self) -> dict[str, int]:
        return {
            "emitted": self.emitted,
            "new": self.new,
            "forced_reingest": self.forced_reingest,
            "duplicate_exact": self.duplicate_exact,
            "skipped_known_remote": self.skipped_known_remote,
            "errors": self.errors,
            "pending": self.pending,
        }


async def run_stats(session: AsyncSession, runs: list[JobRun]) -> dict[int, RunStats]:
    """emitted from the run; the outcomes from ingest_events by job_run_id."""
    ids = [r.id for r in runs]
    counts: dict[int, Counter[str]] = {i: Counter() for i in ids}
    if ids:
        result = await session.execute(
            select(IngestEvent.job_run_id, IngestEvent.outcome, func.count())
            .where(IngestEvent.job_run_id.in_(ids))
            .group_by(IngestEvent.job_run_id, IngestEvent.outcome)
        )
        for run_id, outcome, n in result.all():
            counts[run_id][outcome.value] = n
    return {
        r.id: RunStats(
            emitted=int(r.stats.get("emitted", 0)),
            new=counts[r.id][IngestOutcome.new.value],
            forced_reingest=counts[r.id][IngestOutcome.forced_reingest.value],
            duplicate_exact=counts[r.id][IngestOutcome.duplicate_exact.value],
            skipped_known_remote=counts[r.id][IngestOutcome.skipped_known_remote.value],
            errors=counts[r.id][IngestOutcome.error.value],
        )
        for r in runs
    }


async def list_runs(
    session: AsyncSession, *, source_id: int | None = None, limit: int = 100, offset: int = 0
) -> list[JobRun]:
    stmt = select(JobRun).order_by(JobRun.id.desc()).limit(limit).offset(offset)
    if source_id is not None:
        stmt = stmt.where(JobRun.source_id == source_id)
    return list((await session.scalars(stmt)).all())


async def last_runs(session: AsyncSession, source_ids: list[int]) -> dict[int, JobRun]:
    """The newest run of each source."""
    if not source_ids:
        return {}
    newest = (
        select(func.max(JobRun.id))
        .where(JobRun.source_id.in_(source_ids))
        .group_by(JobRun.source_id)
    )
    rows = await session.scalars(select(JobRun).where(JobRun.id.in_(newest)))
    return {r.source_id: r for r in rows.all() if r.source_id is not None}
