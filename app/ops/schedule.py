"""Schedules of the maintenance jobs, as cron expressions in APP_TIMEZONE.

A periodic task checks them every minute. schedule_state keeps when each one last fired; the
first check only records the current time, so a fresh install does not start every job at
once. A schedule that is due while a run of its kind is active stays due (missed fire times
coalesce into one run), like crawl schedules.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.crawl.scheduling import next_fire
from app.db.models_crawl import JobKind
from app.db.models_ops import ScheduleState
from app.ops.runs import MaintenanceActive, queue_maintenance
from app.ops.settings import EffectiveSettings

logger = logging.getLogger(__name__)

# Fixed local times; the tool update schedule is a setting.
FIXED_SCHEDULES: dict[JobKind, str] = {
    JobKind.purge_trash: "30 3 * * *",  # daily 03:30
    JobKind.backup: "0 4 * * *",  # daily 04:00
    JobKind.housekeeping: "30 4 * * *",  # daily 04:30
    JobKind.integrity: "0 5 * * 0",  # Sundays 05:00
}


def schedules(settings: EffectiveSettings) -> dict[JobKind, str]:
    return {**FIXED_SCHEDULES, JobKind.update_tools: settings.tools_update_schedule}


@dataclass
class MaintenanceTick:
    queued: dict[str, int] = field(default_factory=dict)  # kind -> run id
    skipped_active: list[str] = field(default_factory=list)
    started: list[str] = field(default_factory=list)  # schedules seen for the first time


async def schedule_maintenance(
    session: AsyncSession, settings: EffectiveSettings, now: datetime | None = None
) -> MaintenanceTick:
    """Queue every maintenance run whose schedule is due. Commits."""
    now = now or datetime.now(UTC)
    tick = MaintenanceTick()
    for kind, cron in schedules(settings).items():
        state = await session.get(ScheduleState, kind.value, with_for_update=True)
        if state is None:
            session.add(ScheduleState(name=kind.value, last_fired_at=now))
            await session.commit()
            tick.started.append(kind.value)
            continue
        try:
            due = next_fire(cron, state.last_fired_at, settings.zone) <= now
        except (ValueError, KeyError) as exc:
            logger.warning("invalid schedule %r for %s: %s", cron, kind.value, exc)
            due = False
        await session.commit()
        if not due:
            continue
        try:
            run = await queue_maintenance(session, kind)
        except MaintenanceActive:
            tick.skipped_active.append(kind.value)
            continue
        state = await session.get(ScheduleState, kind.value, with_for_update=True)
        if state is not None:
            state.last_fired_at = now
        await session.commit()
        tick.queued[kind.value] = run.id
        logger.info("queued scheduled %s run %s", kind.value, run.id)
    return tick


async def next_runs(
    session: AsyncSession, settings: EffectiveSettings, now: datetime | None = None
) -> dict[str, datetime]:
    """When each schedule fires next (UTC), for /settings."""
    now = now or datetime.now(UTC)
    out = {}
    for kind, cron in schedules(settings).items():
        state = await session.get(ScheduleState, kind.value)
        base = state.last_fired_at if state is not None else now
        try:
            fire = next_fire(cron, base, settings.zone)
        except (ValueError, KeyError):
            continue
        out[kind.value] = max(fire, now)  # an overdue schedule fires at the next tick
    return out
