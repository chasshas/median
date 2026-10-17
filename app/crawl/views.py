"""Read models for the sources and jobs pages and the JSON API."""

from dataclasses import dataclass
from pathlib import Path

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawl.registry import Registry, get_registry
from app.crawl.service import RunStats, last_runs, run_stats
from app.db.models_crawl import CrawlSource, JobRun
from app.storage import DataPaths

LOG_TAIL_LINES = 200
LOG_TAIL_BYTES = 256 * 1024


def app_registry(request: Request) -> Registry:
    """FastAPI dependency: the add-on registry discovered at startup."""
    registry = getattr(request.app.state, "registry", None)
    return registry if registry is not None else get_registry()


@dataclass
class RunView:
    run: JobRun
    stats: RunStats
    source_name: str | None

    @property
    def duration_seconds(self) -> float | None:
        if self.run.started_at is None:
            return None
        if self.run.finished_at is None:
            from datetime import UTC, datetime

            return (datetime.now(UTC) - self.run.started_at).total_seconds()
        return (self.run.finished_at - self.run.started_at).total_seconds()

    def as_json(self) -> dict[str, object]:
        run = self.run
        return {
            "id": run.id,
            "kind": run.kind.value,
            "status": run.status.value,
            "source_id": run.source_id,
            "source_name": self.source_name,
            "queued_at": run.queued_at.isoformat() if run.queued_at else None,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "duration_seconds": self.duration_seconds,
            "cancel_requested": run.cancel_requested,
            "error": run.error,
            "log_path": run.log_path,
            "stats": self.stats.as_dict(),
        }


async def run_views(session: AsyncSession, runs: list[JobRun]) -> list[RunView]:
    stats = await run_stats(session, runs)
    source_ids = {r.source_id for r in runs if r.source_id is not None}
    names: dict[int, str] = {}
    if source_ids:
        rows = await session.execute(
            select(CrawlSource.id, CrawlSource.name).where(CrawlSource.id.in_(source_ids))
        )
        names = {i: n for i, n in rows.all()}
    return [RunView(r, stats[r.id], names.get(r.source_id) if r.source_id else None) for r in runs]


@dataclass
class SourceView:
    source: CrawlSource
    addon_name: str | None  # None: the add-on is not loaded
    last_run: JobRun | None

    def as_json(self) -> dict[str, object]:
        s = self.source
        last = self.last_run
        return {
            "id": s.id,
            "name": s.name,
            "addon_key": s.addon_key,
            "addon_loaded": self.addon_name is not None,
            "config": s.config,
            "schedule": s.schedule,
            "enabled": s.enabled,
            "default_folder_id": s.default_folder_id,
            "default_tags": s.default_tags,
            "max_runtime_minutes": s.max_runtime_minutes,
            "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
            "created_at": s.created_at.isoformat(),
            "last_run": (
                {
                    "id": last.id,
                    "status": last.status.value,
                    "queued_at": last.queued_at.isoformat(),
                    "finished_at": last.finished_at.isoformat() if last.finished_at else None,
                }
                if last
                else None
            ),
        }


async def source_views(
    session: AsyncSession, registry: Registry, sources: list[CrawlSource]
) -> list[SourceView]:
    last = await last_runs(session, [s.id for s in sources])
    out = []
    for s in sources:
        info = registry.get(s.addon_key)
        out.append(SourceView(s, info.name if info else None, last.get(s.id)))
    return out


def log_tail(paths: DataPaths, run: JobRun, lines: int = LOG_TAIL_LINES) -> str:
    """The last lines of a run's log file ("" if there is none yet)."""
    if not run.log_path:
        return ""
    try:
        path: Path = paths.resolve(run.log_path)
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - LOG_TAIL_BYTES))
            data = fh.read()
    except (OSError, ValueError):
        return ""
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])
