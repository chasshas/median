"""Worker restarts: interrupted runs, stale queue jobs, files stuck in processing."""

import asyncio
import contextlib

from app.jobs import run_queued_jobs
from app.ops.housekeeping import recover_on_startup, requeue_stuck_files
from tests.crawl.helpers import make_source, queue, run_row, wait_for_status
from tests.crawl.test_runner import start_run
from tests.ingest.helpers import one
from tests.review.helpers import pending


def queue_job(conn, run_id: int) -> tuple[int, str]:
    return one(
        conn,
        "SELECT j.id, j.status::text FROM job_runs r JOIN procrastinate_jobs j"
        " ON j.id = r.queue_job_id WHERE r.id = %s",
        (run_id,),
    )


async def test_restart_during_a_crawl_marks_it_interrupted_and_the_next_run_works(
    crawl_app, registry, sessionmaker, paths, db_conn
) -> None:
    source_id = await make_source(
        sessionmaker, registry, paths, addon_key="demo", config={"count": 2}
    )
    run_id = await queue(sessionmaker, source_id)
    # A worker picked the run up and was killed (SIGKILL): both rows stay "running"/"doing".
    job_id, _ = queue_job(db_conn, run_id)
    db_conn.execute("UPDATE procrastinate_jobs SET status = 'doing' WHERE id = %s", (job_id,))
    db_conn.execute(
        "UPDATE job_runs SET status = 'running', started_at = now() WHERE id = %s", (run_id,)
    )
    (stuck, recent) = await pending(sessionmaker, paths, db_conn, 2, seed=40)
    db_conn.execute(
        "UPDATE files SET status = 'processing', collected_at = now() - interval '2 hours'"
        " WHERE id = %s",
        (stuck,),
    )
    db_conn.execute("UPDATE files SET status = 'processing' WHERE id = %s", (recent,))

    result = await recover_on_startup(sessionmaker)

    assert result.interrupted_runs == [run_id]
    assert result.failed_queue_jobs == [job_id]
    assert result.requeued_files == [stuck]
    assert run_row(db_conn, run_id)[:2] == ("failed", "interrupted")
    assert queue_job(db_conn, run_id)[1] == "failed"
    retry = db_conn.execute(
        "SELECT args FROM procrastinate_jobs WHERE task_name = 'retry_file'"
    ).fetchall()
    assert retry == [({"file_id": stuck},)]

    # The source's lock is free again: the next run works, and the stuck file is processed.
    next_run = await queue(sessionmaker, source_id)
    await run_queued_jobs()
    assert run_row(db_conn, next_run)[0] == "succeeded"
    statuses = dict(db_conn.execute("SELECT id, status::text FROM files").fetchall())
    assert statuses[stuck] == "pending_review"
    assert statuses[recent] == "processing"  # younger than an hour: left alone
    # A second recovery finds nothing to do.
    again = await recover_on_startup(sessionmaker)
    assert again.as_dict() == {
        "interrupted_runs": [],
        "failed_queue_jobs": [],
        "requeued_files": [],
    }


async def test_a_stuck_file_with_a_live_job_is_not_requeued_twice(
    crawl_app, sessionmaker, paths, db_conn
) -> None:
    (stuck,) = await pending(sessionmaker, paths, db_conn, 1, seed=50)
    db_conn.execute(
        "UPDATE files SET status = 'processing', collected_at = now() - interval '3 hours'"
        " WHERE id = %s",
        (stuck,),
    )
    assert await requeue_stuck_files(sessionmaker) == [stuck]
    assert await requeue_stuck_files(sessionmaker) == []  # its retry job is still waiting


async def test_worker_shutdown_cancels_a_crawl_as_interrupted(
    crawl_app, registry, sessionmaker, paths, db_conn
) -> None:
    source_id = await make_source(sessionmaker, registry, paths, addon_key="sleeper")
    run_id = await queue(sessionmaker, source_id)
    task = await start_run(sessionmaker, paths, registry, run_id)
    await wait_for_status(db_conn, run_id, {"running"})
    await asyncio.sleep(0.3)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert run_row(db_conn, run_id)[:2] == ("failed", "interrupted")
    assert not paths.work_dir(run_id).exists()
