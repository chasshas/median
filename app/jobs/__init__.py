"""Background jobs facade. The rest of the app uses these functions, never Procrastinate."""

import contextlib
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.app import job_app, make_connector


@contextlib.asynccontextmanager
async def open_jobs(database_url: str) -> AsyncIterator[None]:
    """Open the job queue on database_url for the duration of the block (web process)."""
    with job_app.replace_connector(make_connector(database_url)):
        async with job_app.open_async():
            yield


async def defer_ping(message: str = "ping") -> int:
    """Queue the demo ping task and return the job id."""
    from app.jobs.tasks import ping

    return await ping.defer_async(message=message)


async def job_counts(session: AsyncSession) -> dict[str, int]:
    """Number of jobs per status (todo, doing, succeeded, failed, ...); empty before migrate."""
    exists = await session.scalar(text("SELECT to_regclass('public.procrastinate_jobs')"))
    if exists is None:
        return {}
    result = await session.execute(
        text("SELECT status::text, count(*) FROM procrastinate_jobs GROUP BY status")
    )
    return {status: count for status, count in result.all()}
