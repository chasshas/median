"""python -m app.cli rehash / recompute-similar and the matching Procrastinate tasks."""

import asyncio

import psycopg
import pytest
from fastapi import FastAPI

from app import cli
from app.db.engine import job_sessionmaker
from app.jobs import defer_recompute_similar, defer_rehash, run_queued_jobs, tasks
from app.storage import DataPaths
from tests.ingest.helpers import run
from tests.media import structured_png, variant_jpeg

pytestmark = pytest.mark.usefixtures("clean_ingest")


async def _ingest_pair(database_url: str, paths: DataPaths) -> None:
    async with job_sessionmaker(database_url) as maker:
        original = structured_png(31)
        await run(maker, paths, original, "a.png")
        await run(maker, paths, variant_jpeg(original), "b.jpg")


def test_cli_commands(migrated_db: str, paths: DataPaths, db_conn: psycopg.Connection, capsys):
    asyncio.run(_ingest_pair(migrated_db, paths))
    db_conn.execute("UPDATE files SET phash = NULL")
    db_conn.execute("DELETE FROM similar_pairs")

    assert cli.main(["rehash", "--missing-only"]) == 0
    out = capsys.readouterr().out
    assert "rehash: considered 2, hashed 2" in out
    assert db_conn.execute("SELECT count(*) FROM similar_pairs").fetchone() == (1,)

    db_conn.execute("UPDATE similar_pairs SET dismissed = true")
    assert cli.main(["recompute-similar"]) == 0
    out = capsys.readouterr().out
    assert "recompute-similar: pairs 1, inserted 0, removed 0, dismissed_kept 1" in out

    assert cli.main(["rehash"]) == 0
    assert db_conn.execute("SELECT dismissed FROM similar_pairs").fetchall() == [(True,)]


async def test_tasks(app: FastAPI, sessionmaker, paths, db_conn: psycopg.Connection) -> None:
    original = structured_png(32)
    await run(sessionmaker, paths, original, "a.png")
    await run(sessionmaker, paths, variant_jpeg(original), "b.jpg")
    db_conn.execute("DELETE FROM similar_pairs")

    await defer_recompute_similar()
    await defer_rehash(missing_only=True)
    await run_queued_jobs()
    jobs = db_conn.execute(
        "SELECT task_name, status::text FROM procrastinate_jobs ORDER BY id"
    ).fetchall()
    assert jobs == [("recompute_similar", "succeeded"), ("rehash", "succeeded")]
    assert db_conn.execute("SELECT count(*) FROM similar_pairs").fetchone() == (1,)
    assert (await tasks.rehash(missing_only=True))["considered"] == 0
