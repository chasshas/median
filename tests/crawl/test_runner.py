import asyncio
import time

import httpx
import psycopg
import pytest

from app.crawl import runner, service
from app.jobs import run_queued_jobs
from app.library import folders
from tests.conftest import SAME_ORIGIN
from tests.crawl.helpers import make_source, queue, run_status, wait_for_status
from tests.ingest.helpers import one


@pytest.fixture
async def client(crawl_app):
    transport = httpx.ASGITransport(app=crawl_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=SAME_ORIGIN
    ) as http:
        yield http


async def coast_folder(sessionmaker) -> int:
    async with sessionmaker() as session:
        folder = await folders.create(session, "Coast")
        await session.commit()
        return folder.id


def tags_of(conn: psycopg.Connection, file_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT t.name, ft.origin::text FROM file_tags ft JOIN tags t ON t.id = ft.tag_id"
        " WHERE ft.file_id = %s AND NOT ft.removed",
        (file_id,),
    ).fetchall()
    return dict(rows)


def outcomes(conn: psycopg.Connection, run_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT outcome::text, count(*) FROM ingest_events WHERE job_run_id = %s GROUP BY 1",
        (run_id,),
    ).fetchall()
    return dict(rows)


async def test_demo_run_ingests_and_second_run_skips(
    crawl_app, registry, sessionmaker, paths, db_conn, client
) -> None:
    folder_id = await coast_folder(sessionmaker)
    source_id = await make_source(
        sessionmaker,
        registry,
        paths,
        name="Demo",
        config={"count": 10, "seed": 7, "tags": ["Cats", "outdoor"]},
        default_tags=["Demo Source"],
        default_folder_id=folder_id,
    )
    run_id = await queue(sessionmaker, source_id)
    await run_queued_jobs()

    assert run_status(db_conn, run_id) == "succeeded"
    files = db_conn.execute(
        "SELECT id, status::text, metadata->>'default_folder_id' FROM files ORDER BY id"
    ).fetchall()
    assert len(files) == 10
    assert {f[1] for f in files} == {"pending_review"}
    assert {f[2] for f in files} == {str(folder_id)}
    sources = db_conn.execute(
        "SELECT source_key, remote_id, source_id, job_run_id FROM file_sources ORDER BY remote_id"
    ).fetchall()
    assert len(sources) == 10
    assert {(s[0], s[2], s[3]) for s in sources} == {("demo", source_id, run_id)}
    assert {s[1] for s in sources} == {f"demo-7-{n}" for n in range(1, 11)}
    for file_id, *_ in files:
        assert tags_of(db_conn, file_id) == {
            "cats": "crawler",
            "outdoor": "crawler",
            "demo_source": "crawler",
        }
    assert outcomes(db_conn, run_id) == {"new": 10}
    assert not paths.work_dir(run_id).exists()
    log = paths.job_log(run_id).read_text()
    assert "generating 10 images with seed 7" in log
    assert f"run {run_id} succeeded (emitted 10)" in log

    detail = (await client.get(f"/api/jobs/{run_id}")).json()
    assert detail["stats"] == {
        "emitted": 10,
        "new": 10,
        "forced_reingest": 0,
        "duplicate_exact": 0,
        "skipped_known_remote": 0,
        "errors": 0,
        "pending": 0,
    }
    assert detail["log_path"] == f"logs/jobs/{run_id}.log"

    # The review queue offers the source's default folder to the archive panel.
    queue_page = (await client.get("/api/review/queue")).json()
    assert queue_page["total"] == 10
    assert {i["default_folder_id"] for i in queue_page["items"]} == {folder_id}

    second = await queue(sessionmaker, source_id)
    await run_queued_jobs()
    assert run_status(db_conn, second) == "succeeded"
    assert outcomes(db_conn, second) == {"skipped_known_remote": 10}
    assert one(db_conn, "SELECT count(*) FROM files")[0] == 10
    stats = (await client.get(f"/api/jobs/{second}")).json()["stats"]
    assert (stats["emitted"], stats["skipped_known_remote"], stats["new"]) == (10, 10, 0)
    assert not list((paths.staging / f"run-{second}").glob("*"))


async def test_source_without_default_folder(crawl_app, registry, sessionmaker, paths, client):
    source_id = await make_source(sessionmaker, registry, paths, config={"count": 1})
    await queue(sessionmaker, source_id)
    await run_queued_jobs()
    item = (await client.get("/api/review/queue")).json()["items"][0]
    assert item["default_folder_id"] is None


async def test_known_remote_lookups(crawl_app, registry, sessionmaker, paths, db_conn) -> None:
    source_id = await make_source(sessionmaker, registry, paths, config={"count": 3, "seed": 1})
    await queue(sessionmaker, source_id)
    await run_queued_jobs()
    db_conn.execute("UPDATE files SET status = 'discarded'")
    async with sessionmaker() as session:
        source = await service.get_source(session, source_id)
    backend = runner.RunBackend(sessionmaker, paths, source, 0)

    assert await backend.is_known("demo-1-2")  # any status counts, discarded included
    assert not await backend.is_known("demo-1-9")
    assert await backend.known_remote_ids() == {"demo-1-1", "demo-1-2", "demo-1-3"}
    other = runner.RunBackend(
        sessionmaker, paths, source.__class__(id=99, addon_key="other", default_tags=[]), 0
    )
    assert await other.known_remote_ids() == set()


async def start_run(sessionmaker, paths, registry, run_id: int) -> asyncio.Task:
    return asyncio.create_task(runner.run_crawl(sessionmaker, paths, registry, run_id))


async def test_cancel_running_demo_run(
    crawl_app, registry, sessionmaker, paths, db_conn, client
) -> None:
    source_id = await make_source(
        sessionmaker, registry, paths, config={"count": 200, "delay_seconds": 0.2}
    )
    run_id = await queue(sessionmaker, source_id)
    task = await start_run(sessionmaker, paths, registry, run_id)
    await wait_for_status(db_conn, run_id, {"running"})
    await asyncio.sleep(0.6)

    started = time.monotonic()
    reply = await client.post(f"/api/jobs/{run_id}/cancel")
    assert reply.status_code == 200
    assert reply.json()["cancel_requested"] is True
    status = await asyncio.wait_for(task, timeout=5)
    elapsed = time.monotonic() - started

    assert status.value == "cancelled"
    assert elapsed < 3  # seen within 2 s, then the add-on returns on its own
    row = one(
        db_conn, "SELECT status::text, finished_at, stats FROM job_runs WHERE id = %s", (run_id,)
    )
    assert row[0] == "cancelled" and row[1] is not None
    assert 0 < row[2]["emitted"] < 200
    assert not paths.work_dir(run_id).exists()
    assert "cancel requested" in paths.job_log(run_id).read_text()
    again = await client.post(f"/api/jobs/{run_id}/cancel")
    assert again.status_code == 409


async def test_cancel_kills_subprocesses(crawl_app, registry, sessionmaker, paths, db_conn):
    source_id = await make_source(sessionmaker, registry, paths, addon_key="sleeper")
    run_id = await queue(sessionmaker, source_id)
    task = await start_run(sessionmaker, paths, registry, run_id)
    log = paths.job_log(run_id)
    for _ in range(100):
        if log.exists() and "| child " in log.read_text():
            break
        await asyncio.sleep(0.05)
    child = int(log.read_text().split("| child ")[1].split()[0])

    async with sessionmaker() as session:
        await service.cancel_run(session, run_id)
    status = await asyncio.wait_for(task, timeout=8)

    assert status.value == "cancelled"
    await asyncio.sleep(0.2)
    from tests.crawl.test_procs import alive

    assert not alive(child)
    assert "stopping process group" in log.read_text()


async def test_stubborn_addon_is_stopped(
    crawl_app, registry, sessionmaker, paths, db_conn, monkeypatch
):
    monkeypatch.setattr(runner, "STOP_GRACE_SECONDS", 0.5)
    source_id = await make_source(sessionmaker, registry, paths, addon_key="stubborn")
    run_id = await queue(sessionmaker, source_id)
    task = await start_run(sessionmaker, paths, registry, run_id)
    await wait_for_status(db_conn, run_id, {"running"})
    async with sessionmaker() as session:
        await service.cancel_run(session, run_id)
    assert (await asyncio.wait_for(task, timeout=6)).value == "cancelled"


async def test_max_runtime_is_enforced(
    crawl_app, registry, sessionmaker, paths, db_conn, monkeypatch
):
    monkeypatch.setattr(runner, "SECONDS_PER_MINUTE", 0.5)
    source_id = await make_source(
        sessionmaker, registry, paths, addon_key="sleeper", max_runtime_minutes=1
    )
    run_id = await queue(sessionmaker, source_id)
    started = time.monotonic()
    status = await runner.run_crawl(sessionmaker, paths, registry, run_id)

    assert status.value == "failed"
    assert time.monotonic() - started < 5
    error = one(db_conn, "SELECT error FROM job_runs WHERE id = %s", (run_id,))[0]
    assert error == "exceeded the maximum runtime of 1 minutes"
    assert not paths.work_dir(run_id).exists()


async def test_failing_addon(crawl_app, registry, sessionmaker, paths, db_conn) -> None:
    source_id = await make_source(sessionmaker, registry, paths, addon_key="failing")
    run_id = await queue(sessionmaker, source_id)
    status = await runner.run_crawl(sessionmaker, paths, registry, run_id)

    assert status.value == "failed"
    row = one(
        db_conn, "SELECT error, started_at, finished_at FROM job_runs WHERE id = %s", (run_id,)
    )
    assert row[0] == "RuntimeError: the site changed"
    assert row[1] is not None and row[2] is not None
    assert not paths.work_dir(run_id).exists()
    assert "Traceback" in paths.job_log(run_id).read_text()


async def test_missing_addon_and_bad_config(crawl_app, registry, sessionmaker, paths, db_conn):
    source_id = await make_source(sessionmaker, registry, paths, config={"count": 2})
    db_conn.execute(
        "UPDATE crawl_sources SET config = '{\"count\": -1}' WHERE id = %s", (source_id,)
    )
    run_id = await queue(sessionmaker, source_id)
    await run_queued_jobs()
    assert run_status(db_conn, run_id) == "failed"
    assert (
        "invalid configuration"
        in one(db_conn, "SELECT error FROM job_runs WHERE id = %s", (run_id,))[0]
    )

    db_conn.execute("UPDATE crawl_sources SET addon_key = 'gone' WHERE id = %s", (source_id,))
    run_id = await queue(sessionmaker, source_id)
    await run_queued_jobs()
    assert run_status(db_conn, run_id) == "failed"
    assert one(db_conn, "SELECT error FROM job_runs WHERE id = %s", (run_id,))[0] == (
        "add-on 'gone' is not loaded"
    )


async def test_cancel_queued_run(crawl_app, registry, sessionmaker, paths, db_conn) -> None:
    source_id = await make_source(sessionmaker, registry, paths, config={"count": 2})
    run_id = await queue(sessionmaker, source_id)
    async with sessionmaker() as session:
        run = await service.cancel_run(session, run_id)
    assert run.status.value == "cancelled"
    queue_job = one(
        db_conn,
        "SELECT status::text FROM procrastinate_jobs"
        " WHERE id = (SELECT queue_job_id FROM job_runs WHERE id = %s)",
        (run_id,),
    )[0]
    assert queue_job == "cancelled"
    # A worker that picks the run up anyway does nothing.
    assert (await runner.run_crawl(sessionmaker, paths, registry, run_id)).value == "cancelled"
    assert one(db_conn, "SELECT count(*) FROM files")[0] == 0
    # The source can be queued again at once.
    await queue(sessionmaker, source_id)


async def test_lost_runs_are_reaped(crawl_app, registry, sessionmaker, paths, db_conn) -> None:
    source_id = await make_source(sessionmaker, registry, paths, config={"count": 1})
    run_id = await queue(sessionmaker, source_id)
    # The worker died: the run row says running, its queue job failed.
    db_conn.execute("UPDATE job_runs SET status = 'running' WHERE id = %s", (run_id,))
    db_conn.execute(
        "UPDATE procrastinate_jobs SET status = 'failed' WHERE id ="
        " (SELECT queue_job_id FROM job_runs WHERE id = %s)",
        (run_id,),
    )
    async with sessionmaker() as session:
        assert await service.reap_lost_runs(session) == [run_id]
    assert run_status(db_conn, run_id) == "failed"
    await queue(sessionmaker, source_id)  # no longer blocks the source
