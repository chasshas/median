import uuid
from pathlib import Path

import psycopg

from app.ingest.origin import IngestOrigin
from app.ingest.process import process_ingest
from app.storage import DataPaths
from tests.media import write


def stage(paths: DataPaths, data: bytes, name: str, batch_id: str | None = None) -> str:
    """Put bytes into staging/<batch>/<name> as ingest() would; return the relative path."""
    batch_id = batch_id or uuid.uuid4().hex
    dest = write(paths.staging_batch(batch_id) / name, data)
    return paths.relative(dest)


def stage_file(paths: DataPaths, src: Path, batch_id: str | None = None) -> str:
    return stage(paths, src.read_bytes(), src.name, batch_id)


async def run(sessionmaker, paths: DataPaths, data: bytes, name: str, origin=None, batch=None):
    rel = stage(paths, data, name, batch)
    outcome = await process_ingest(
        sessionmaker, paths, rel, origin or IngestOrigin(), name, rel.split("/")[1]
    )
    return outcome, rel


def one(conn: psycopg.Connection, sql: str, params: tuple = ()):
    row = conn.execute(sql, params).fetchone()
    assert row is not None, sql
    return row


def events(conn: psycopg.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT outcome::text FROM ingest_events ORDER BY id")]
