from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.crawl import service
from app.crawl.scheduling import is_due, next_fire, schedule_due_runs
from app.jobs import run_queued_jobs
from tests.crawl.helpers import make_source, run_status

SEOUL = ZoneInfo("Asia/Seoul")
T0 = datetime(2026, 10, 17, 3, 0, 30, tzinfo=UTC)


def test_next_fire_every_minute() -> None:
    assert next_fire("* * * * *", T0, SEOUL) == datetime(2026, 10, 17, 3, 1, tzinfo=UTC)


def test_schedules_are_read_in_app_timezone() -> None:
    # 09:00 in Seoul is 00:00 UTC.
    assert next_fire("0 9 * * *", T0, SEOUL) == datetime(2026, 10, 18, 0, 0, tzinfo=UTC)
    assert next_fire("0 9 * * *", T0, ZoneInfo("UTC")) == datetime(2026, 10, 17, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("schedule", "last", "now", "due"),
    [
        ("* * * * *", None, T0 + timedelta(seconds=10), False),  # created 03:00:30
        ("* * * * *", None, T0 + timedelta(seconds=30), True),  # 03:01:00
        ("* * * * *", T0 + timedelta(seconds=31), T0 + timedelta(seconds=40), False),
        ("* * * * *", T0 + timedelta(seconds=31), T0 + timedelta(seconds=90), True),
        ("*/30 * * * *", T0, T0 + timedelta(minutes=20), False),
        ("*/30 * * * *", T0, T0 + timedelta(minutes=29, seconds=30), True),
        # Many missed fire times: still one run (coalesced).
        ("* * * * *", T0, T0 + timedelta(days=3), True),
    ],
)
def test_is_due(schedule: str, last, now, due: bool) -> None:
    assert is_due(schedule, last, T0, now, SEOUL) is due


def test_invalid_schedules_are_rejected() -> None:
    assert service.validate_schedule("  */5  * * * * ") == "*/5 * * * *"
    assert service.validate_schedule("") is None
    for bad in ("every minute", "* * * *", "61 * * * *", "* * * * * *"):
        with pytest.raises(service.SourceError):
            service.validate_schedule(bad)


async def _set(db_conn, source_id: int, **values) -> None:
    for key, value in values.items():
        db_conn.execute(f"UPDATE crawl_sources SET {key} = %s WHERE id = %s", (value, source_id))


async def test_tick_queues_due_sources_only(crawl_app, registry, sessionmaker, paths, db_conn):
    due = await make_source(sessionmaker, registry, paths, name="due", schedule="* * * * *")
    later = await make_source(sessionmaker, registry, paths, name="later", schedule="0 3 1 1 *")
    disabled = await make_source(
        sessionmaker, registry, paths, name="off", schedule="* * * * *", enabled=False
    )
    manual = await make_source(sessionmaker, registry, paths, name="manual")
    created = T0 - timedelta(minutes=5)
    for sid in (due, later, disabled, manual):
        await _set(db_conn, sid, created_at=created)

    async with sessionmaker() as session:
        result = await schedule_due_runs(session, SEOUL, now=T0)
    assert list(result.queued) == [due]
    run_id = result.queued[due]
    assert run_status(db_conn, run_id) == "queued"
    rows = db_conn.execute("SELECT source_id FROM job_runs").fetchall()
    assert rows == [(due,)]

    # The next tick in the same minute queues nothing (last_run_at moved on).
    async with sessionmaker() as session:
        again = await schedule_due_runs(session, SEOUL)
    assert again.queued == {}


async def test_no_overlapping_runs(crawl_app, registry, sessionmaker, paths, db_conn) -> None:
    source_id = await make_source(sessionmaker, registry, paths, schedule="* * * * *")
    await _set(db_conn, source_id, created_at=T0 - timedelta(minutes=5))
    async with sessionmaker() as session:
        first = await schedule_due_runs(session, SEOUL, now=T0)
    run_id = first.queued[source_id]

    # Due again a minute later, but the first run is still queued: skipped.
    await _set(db_conn, source_id, last_run_at=T0 - timedelta(minutes=2))
    async with sessionmaker() as session:
        second = await schedule_due_runs(session, SEOUL, now=T0 + timedelta(minutes=1))
    assert second.queued == {}
    assert second.skipped_active == [source_id]

    # Running counts too, and Run now is refused under the same rule.
    db_conn.execute("UPDATE job_runs SET status = 'running' WHERE id = %s", (run_id,))
    async with sessionmaker() as session:
        with pytest.raises(service.RunActive) as exc:
            await service.queue_run(session, source_id)
    assert exc.value.run_id == run_id
    db_conn.execute("UPDATE job_runs SET status = 'queued' WHERE id = %s", (run_id,))

    # Once the run has finished, the next tick queues one new run.
    await run_queued_jobs()
    assert run_status(db_conn, run_id) == "succeeded"
    async with sessionmaker() as session:
        third = await schedule_due_runs(session, SEOUL, now=T0 + timedelta(minutes=2))
    assert list(third.queued) == [source_id]
    assert db_conn.execute("SELECT count(*) FROM job_runs").fetchone()[0] == 2


async def test_scheduled_task_runs_the_scheduler(crawl_app, registry, sessionmaker, paths, db_conn):
    from app.jobs.tasks import schedule_crawls

    source_id = await make_source(sessionmaker, registry, paths, schedule="* * * * *")
    await _set(db_conn, source_id, created_at=datetime.now(UTC) - timedelta(minutes=2))
    result = await schedule_crawls(timestamp=0)
    assert list(result["queued"]) == [source_id]

    from app.jobs.app import job_app

    periodic = {p.task.name: p.cron for p in job_app.periodic_registry.periodic_tasks.values()}
    assert periodic["schedule_crawls"] == "* * * * *"
