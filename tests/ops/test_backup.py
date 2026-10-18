import logging
import subprocess
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from app.jobs import run_queued_jobs
from app.jobs.tasks import backup as backup_task
from app.ops.backup import (
    apply_retention,
    backup_database,
    dump_name,
    list_dumps,
    pg_env,
    restore_database,
)
from app.ops.settings import save_settings
from tests.media import write

pytestmark = pytest.mark.usefixtures("clean_ingest")
log = logging.getLogger("test.backup")


def pg_restore_list(path, env) -> str:
    result = subprocess.run(
        ["pg_restore", "--list", str(path)], env=env, capture_output=True, text=True, check=True
    )
    return result.stdout


async def test_backup_creates_a_dump_pg_restore_can_read(migrated_db, paths, db_conn) -> None:
    db_conn.execute("INSERT INTO tags (name) VALUES ('backup-marker')")
    result = await backup_database(migrated_db, paths, keep=14, log=log)
    dump = paths.root / result.file
    assert dump.is_file() and dump.parent == paths.backups
    assert dump.name.startswith("db-") and dump.suffix == ".dump"
    listing = pg_restore_list(dump, pg_env(migrated_db))
    assert "TABLE public files" in listing and "TABLE DATA public tags" in listing
    assert result.entries > 10 and result.size_bytes == dump.stat().st_size
    assert not list(paths.backups.glob(".*.part"))


def test_pg_env_keeps_the_password_out_of_arguments() -> None:
    env = pg_env("postgresql://me:s3cret@db.lan:5433/media")
    assert env["PGPASSWORD"] == "s3cret" and env["PGHOST"] == "db.lan"
    assert env["PGPORT"] == "5433" and env["PGDATABASE"] == "media" and env["PGUSER"] == "me"


def test_retention_keeps_the_newest(paths) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    names = [dump_name(start + timedelta(days=i)) for i in range(5)]
    for name in names:
        write(paths.backups / name, b"x")
    write(paths.backups / "notes.txt", b"keep me")
    deleted = apply_retention(paths, 2)
    assert sorted(deleted) == sorted(names[:3])
    assert [p.name for p in list_dumps(paths)] == [names[4], names[3]]
    assert (paths.backups / "notes.txt").is_file()


async def test_backup_job_records_a_run_and_applies_the_setting(
    client, sessionmaker, paths, db_conn
) -> None:
    async with sessionmaker() as session:
        await save_settings(session, {"backup_retention": 2})
    start = datetime(2020, 1, 1, tzinfo=UTC)
    for i in range(3):
        write(paths.backups / dump_name(start + timedelta(days=i)), b"old")

    r = await client.post("/settings/run/backup")
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    await run_queued_jobs()
    status, stats, log_path = db_conn.execute(
        "SELECT status::text, stats, log_path FROM job_runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert status == "succeeded"
    assert stats["kept"] == 2 and len(stats["deleted"]) == 2
    dumps = list_dumps(paths)
    assert dumps[0].name == stats["file"].split("/")[-1]
    assert len(dumps) == 2
    assert (paths.root / log_path).read_text().count("pg_dump") >= 1
    page = await client.get("/settings")
    assert stats["file"].split("/")[-1] in page.text
    jobs = await client.get("/jobs?kind=backup")
    assert f'data-run-id="{run_id}"' in jobs.text and "data-summary" in jobs.text


async def test_backup_failure_marks_the_run_failed(
    sessionmaker, paths, db_conn, monkeypatch
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    assert await backup_task(job_run_id=None) == "failed"
    error = db_conn.execute("SELECT error FROM job_runs WHERE kind = 'backup'").fetchone()[0]
    assert "pg_dump is not installed" in error


@pytest.fixture
def scratch_db(migrated_db):
    """An empty extra database next to the test database, dropped afterwards."""
    parts = urlsplit(migrated_db)
    name = parts.path.lstrip("/") + "_restore"
    admin = urlunsplit(parts._replace(path="/postgres"))
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        conn.execute(f'CREATE DATABASE "{name}"')
    yield urlunsplit(parts._replace(path=f"/{name}"))
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


async def test_restore_brings_back_the_data(migrated_db, scratch_db, paths, db_conn) -> None:
    db_conn.execute("INSERT INTO tags (name) VALUES ('restored-marker')")
    result = await backup_database(migrated_db, paths, keep=5, log=log)
    with psycopg.connect(scratch_db, autocommit=True) as conn:
        conn.execute("CREATE TABLE leftover (x int)")  # replaced by the restore
    migrations = []
    entries = restore_database(
        scratch_db, paths.root / result.file, migrate=migrations.append, echo=lambda _: None
    )
    assert entries == result.entries and migrations == [scratch_db]
    with psycopg.connect(scratch_db) as conn:
        tags = [r[0] for r in conn.execute("SELECT name FROM tags").fetchall()]
        assert "restored-marker" in tags
        assert conn.execute("SELECT to_regclass('public.leftover')").fetchone() == (None,)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0007",)
        assert conn.execute("SELECT count(*) FROM folders WHERE name = 'Inbox'").fetchone() == (1,)


def test_restore_refuses_an_unreadable_dump_before_touching_anything(scratch_db, paths) -> None:
    from app.ops.backup import BackupError

    with psycopg.connect(scratch_db, autocommit=True) as conn:
        conn.execute("CREATE TABLE precious (x int)")
    bad = write(paths.backups / "db-20260101T000000Z.dump", b"not a dump")
    with pytest.raises(BackupError):
        restore_database(scratch_db, bad, migrate=lambda _: None, echo=lambda _: None)
    with psycopg.connect(scratch_db) as conn:
        assert conn.execute("SELECT to_regclass('public.precious')").fetchone() == ("precious",)
