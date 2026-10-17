import asyncio
import itertools
import sys
import time
from pathlib import Path

import psycopg

from app.crawl import service
from app.crawl.registry import Registry
from app.storage import DataPaths


async def make_source(sessionmaker, registry: Registry, paths: DataPaths, **fields) -> int:
    data = service.SourceInput(
        name=fields.pop("name", "src"), addon_key=fields.pop("addon_key", "demo"), **fields
    )
    async with sessionmaker() as session:
        source = await service.create_source(session, registry, paths, data)
        return source.id


async def queue(sessionmaker, source_id: int) -> int:
    async with sessionmaker() as session:
        run = await service.queue_run(session, source_id)
        return run.id


def run_status(conn: psycopg.Connection, run_id: int) -> str:
    row = conn.execute("SELECT status::text FROM job_runs WHERE id = %s", (run_id,)).fetchone()
    assert row is not None
    return row[0]


async def wait_for_status(conn, run_id: int, wanted: set[str], limit: float = 10) -> str:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        status = run_status(conn, run_id)
        if status in wanted:
            return status
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} stayed {run_status(conn, run_id)}")


FAKES = Path(__file__).parent / "fakes"
_names = itertools.count(1)


def fake_tool(directory: Path, name: str) -> str:
    """An executable copy of tests/crawl/fakes/<name>.py run by this test's Python."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text(f"#!{sys.executable}\n" + (FAKES / f"{name}.py").read_text())
    target.chmod(0o755)
    return str(target)


async def crawl(sessionmaker, registry: Registry, paths: DataPaths, addon_key: str, config) -> int:
    """Create a source, queue a run, run every queued job (crawl and ingest); the run id."""
    from app.jobs import run_queued_jobs

    name = f"{addon_key} source {next(_names)}"
    source_id = await make_source(
        sessionmaker, registry, paths, name=name, addon_key=addon_key, config=config
    )
    run_id = await queue(sessionmaker, source_id)
    await run_queued_jobs()
    return run_id


async def recrawl(sessionmaker, source_id: int) -> int:
    from app.jobs import run_queued_jobs

    run_id = await queue(sessionmaker, source_id)
    await run_queued_jobs()
    return run_id


def run_row(conn: psycopg.Connection, run_id: int) -> tuple[str, str | None, int, int]:
    """(status, error, source_id, emitted) of a run."""
    row = conn.execute(
        "SELECT status::text, error, source_id, coalesce((stats->>'emitted')::int, 0)"
        " FROM job_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    assert row is not None
    return row


def outcomes(conn: psycopg.Connection, run_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT outcome::text, count(*) FROM ingest_events WHERE job_run_id = %s GROUP BY 1",
        (run_id,),
    ).fetchall()
    return dict(rows)


def crawled_files(conn: psycopg.Connection, run_id: int) -> dict[str, dict]:
    """remote_id -> file row details (url, title, tags, metadata, size) for one run."""
    rows = conn.execute(
        "SELECT s.remote_id, s.url, f.title, f.description, s.raw_metadata, f.size_bytes,"
        " (SELECT coalesce(array_agg(t.name ORDER BY t.name), '{}') FROM file_tags ft"
        "  JOIN tags t ON t.id = ft.tag_id WHERE ft.file_id = f.id AND NOT ft.removed)"
        " FROM file_sources s JOIN files f ON f.id = s.file_id WHERE s.job_run_id = %s",
        (run_id,),
    ).fetchall()
    return {
        r[0]: {
            "url": r[1],
            "title": r[2],
            "description": r[3],
            "metadata": r[4],
            "size": r[5],
            "tags": list(r[6]),
        }
        for r in rows
    }
