"""Cron schedules of crawl sources (croniter). Schedules are read in APP_TIMEZONE."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawl.service import RunActive, queue_run, reap_lost_runs
from app.db.models_crawl import CrawlSource

logger = logging.getLogger(__name__)


def next_fire(schedule: str, after: datetime, tz: ZoneInfo) -> datetime:
    """The first time strictly after `after` that matches the schedule (UTC)."""
    base = after.astimezone(tz)
    fire: datetime = croniter(schedule, base).get_next(datetime)
    return fire.astimezone(UTC)


def is_due(
    schedule: str, last_run_at: datetime | None, created_at: datetime, now: datetime, tz: ZoneInfo
) -> bool:
    """Due when a fire time lies after the last queued run (or the creation) and not after now.

    Missed fire times (the worker was down, or a run was still active) coalesce into one run.
    """
    return next_fire(schedule, last_run_at or created_at, tz) <= now


@dataclass
class TickResult:
    queued: dict[int, int] = field(default_factory=dict)  # source id -> run id
    skipped_active: list[int] = field(default_factory=list)  # due, but a run is active
    reaped: list[int] = field(default_factory=list)  # lost runs marked failed


async def schedule_due_runs(
    session: AsyncSession, tz: ZoneInfo, now: datetime | None = None
) -> TickResult:
    """Queue a run for every enabled source whose cron schedule is due. Commits per source."""
    now = now or datetime.now(UTC)
    result = TickResult(reaped=await reap_lost_runs(session))
    sources = (
        await session.scalars(
            select(CrawlSource)
            .where(CrawlSource.enabled.is_(True), CrawlSource.schedule.is_not(None))
            .order_by(CrawlSource.id)
        )
    ).all()
    due = []
    for source in sources:
        try:
            if is_due(source.schedule or "", source.last_run_at, source.created_at, now, tz):
                due.append(source.id)
        except (ValueError, KeyError) as exc:
            logger.warning(
                "source %s has an invalid schedule %r: %s", source.id, source.schedule, exc
            )
    await session.commit()
    for source_id in due:
        try:
            run = await queue_run(session, source_id)
        except RunActive:
            result.skipped_active.append(source_id)
            logger.info("source %s is due but a run is still active; skipped", source_id)
            continue
        result.queued[source_id] = run.id
        logger.info("queued scheduled run %s of source %s", run.id, source_id)
    return result
