import asyncio
import time

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
