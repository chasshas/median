import logging
import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models_crawl import JobKind
from app.jobs import run_queued_jobs
from app.ops import housekeeping as hk
from app.ops.runs import MaintenanceActive, queue_maintenance
from app.ops.schedule import schedule_maintenance
from app.ops.settings import load_settings, save_settings
from tests.media import write

pytestmark = pytest.mark.usefixtures("clean_ingest", "app")  # app: the job queue is open
log = logging.getLogger("test.maintenance")


def kinds_queued(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT kind::text FROM job_runs ORDER BY id").fetchall()]


async def test_schedules_start_quietly_then_fire_in_local_time(sessionmaker, db_conn) -> None:
    # Saturday 2026-10-17 18:00 UTC = Sunday 03:00 in Asia/Seoul.
    start = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)
    async with sessionmaker() as session:
        settings = await load_settings(session)
        first = await schedule_maintenance(session, settings, now=start)
    assert sorted(first.started) == sorted(
        ["purge_trash", "backup", "housekeeping", "integrity", "update_tools"]
    )
    assert kinds_queued(db_conn) == []  # a fresh install does not run everything at once

    async with sessionmaker() as session:
        tick = await schedule_maintenance(session, settings, now=start + timedelta(minutes=29))
        assert tick.queued == {}
        # 03:31 local: purge_trash (03:30) is due.
        tick = await schedule_maintenance(session, settings, now=start + timedelta(minutes=31))
        assert list(tick.queued) == ["purge_trash"]
        # 05:01 local on a Sunday: backup, housekeeping and integrity; purge_trash already ran.
        tick = await schedule_maintenance(
            session, settings, now=start + timedelta(hours=2, minutes=1)
        )
        assert sorted(tick.queued) == ["backup", "housekeeping", "integrity"]
        again = await schedule_maintenance(
            session, settings, now=start + timedelta(hours=2, minutes=2)
        )
        assert again.queued == {}
    assert kinds_queued(db_conn) == ["purge_trash", "backup", "housekeeping", "integrity"]


async def test_a_due_schedule_waits_for_the_active_run(sessionmaker, db_conn) -> None:
    start = datetime(2026, 10, 18, 0, 0, tzinfo=UTC)  # 09:00 in Seoul
    async with sessionmaker() as session:
        await save_settings(session, {"tools_update_schedule": "30 9 * * *"})
        settings = await load_settings(session)
        await schedule_maintenance(session, settings, now=start)
        active_id = (await queue_maintenance(session, JobKind.update_tools)).id
        tick = await schedule_maintenance(session, settings, now=start + timedelta(minutes=31))
        assert tick.skipped_active == ["update_tools"]
        with pytest.raises(MaintenanceActive):
            await queue_maintenance(session, JobKind.update_tools)
    db_conn.execute("UPDATE job_runs SET status = 'succeeded' WHERE id = %s", (active_id,))
    db_conn.execute("UPDATE procrastinate_jobs SET status = 'succeeded'")  # the run finished
    async with sessionmaker() as session:
        tick = await schedule_maintenance(session, settings, now=start + timedelta(minutes=40))
    assert list(tick.queued) == ["update_tools"]  # missed fire time coalesced into one run


async def test_housekeeping_deletes_old_runs_and_logs_but_keeps_events(
    sessionmaker, paths, db_conn
) -> None:
    old = db_conn.execute(
        "INSERT INTO job_runs (kind, status, queued_at, log_path) VALUES"
        " ('backup', 'succeeded', now() - interval '91 days', 'logs/jobs/placeholder.log')"
        " RETURNING id"
    ).fetchone()[0]
    db_conn.execute(
        "UPDATE job_runs SET log_path = %s WHERE id = %s", (f"logs/jobs/{old}.log", old)
    )
    recent = db_conn.execute(
        "INSERT INTO job_runs (kind, status, queued_at) VALUES"
        " ('backup', 'failed', now() - interval '89 days') RETURNING id"
    ).fetchone()[0]
    db_conn.execute(
        "INSERT INTO ingest_events (outcome, source_key, job_run_id) VALUES ('error', 'demo', %s)",
        (old,),
    )
    write(paths.job_log(old), b"old log")
    write(paths.job_log(recent), b"recent log")
    stray = write(paths.job_logs / "123456.log", b"no run")
    then = time.time() - 100 * 86400
    os.utime(stray, (then, then))
    write(paths.archive / "aa" / "bb" / "x.png", b"12345")
    write(paths.thumbs / "aa" / "t.webp", b"12")
    write(paths.root / "README.txt", b"1234567")

    stats = await hk.housekeeping(sessionmaker, paths, log)

    assert stats["deleted_runs"] == 1 and stats["deleted_logs"] == 2
    ids = [r[0] for r in db_conn.execute("SELECT id FROM job_runs").fetchall()]
    assert ids == [recent]
    assert not paths.job_log(old).exists() and paths.job_log(recent).exists()
    assert db_conn.execute("SELECT job_run_id FROM ingest_events").fetchall() == [(None,)]
    areas = stats["disk"]["areas"]
    assert areas["archive"] == 5 and areas["thumbs"] == 2 and areas["other"] >= 7
    assert set(areas) == {*hk.AREAS, "other"}
    assert stats["disk"]["total_bytes"] > 0 and 0 <= stats["disk"]["free_ratio"] <= 1


async def test_dashboard_shows_disk_usage_and_warns_when_low(
    client, sessionmaker, paths, db_conn, monkeypatch
) -> None:
    page = await client.get("/")
    assert 'id="disk-usage"' in page.text and 'id="disk-areas"' not in page.text
    assert 'id="low-space-warning"' not in page.text

    write(paths.archive / "aa" / "bb" / "x.png", b"12345")
    r = await client.post("/settings/run/housekeeping")
    assert r.status_code == 303
    await run_queued_jobs()
    page = await client.get("/")
    assert 'data-area="archive">5 B<' in page.text

    from app.web import pages

    monkeypatch.setattr(
        pages,
        "free_space",
        lambda p: {
            "total_bytes": 1000,
            "used_bytes": 950,
            "free_bytes": 50,
            "free_ratio": 0.05,
            "low": True,
        },
    )
    page = await client.get("/")
    assert 'id="low-space-warning"' in page.text and "5.0 percent" in page.text


async def test_jobs_page_filters_by_kind_and_status(client, db_conn) -> None:
    rows = {}
    for kind, status in [("backup", "succeeded"), ("integrity", "failed"), ("backup", "failed")]:
        rows[(kind, status)] = db_conn.execute(
            "INSERT INTO job_runs (kind, status) VALUES (%s, %s) RETURNING id", (kind, status)
        ).fetchone()[0]

    def shown(text: str) -> set[int]:
        return {i for i in rows.values() if f'data-run-id="{i}"' in text}

    assert shown((await client.get("/jobs")).text) == set(rows.values())
    assert shown((await client.get("/jobs?kind=backup")).text) == {
        rows[("backup", "succeeded")],
        rows[("backup", "failed")],
    }
    assert shown((await client.get("/jobs?kind=backup&status=failed")).text) == {
        rows[("backup", "failed")]
    }
    assert shown(
        (await client.get("/jobs?status=failed&status=succeeded&kind=integrity")).text
    ) == {rows[("integrity", "failed")]}
    page = await client.get("/jobs?kind=backup")
    assert 'name="kind" value="backup" checked' in page.text
    api = await client.get("/api/jobs", params={"kind": "integrity"})
    assert [r["id"] for r in api.json()["runs"]] == [rows[("integrity", "failed")]]
    assert (await client.get("/api/jobs", params={"status": "nope"})).status_code == 422


def test_cli_runs_record_job_runs(db_conn, capsys) -> None:
    from app.cli import main

    assert main(["integrity", "--verify-hashes"]) == 0
    assert main(["backup"]) == 0
    assert main(["housekeeping"]) == 0
    out = capsys.readouterr().out
    assert "integrity: succeeded" in out and "backup: succeeded" in out
    rows = db_conn.execute("SELECT kind::text, status::text FROM job_runs ORDER BY id").fetchall()
    assert rows == [
        ("integrity", "succeeded"),
        ("backup", "succeeded"),
        ("housekeeping", "succeeded"),
    ]
