from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI

from app import cli
from app.ingest.origin import IngestOrigin
from app.ingest.service import batch_id_for, ingest
from app.jobs import run_queued_jobs
from tests.media import make_audio, make_video, png_bytes, write

pytestmark = pytest.mark.usefixtures("clean_ingest")


def test_batch_ids() -> None:
    assert batch_id_for(IngestOrigin(job_run_id=7)) == "run-7"
    assert len(batch_id_for(IngestOrigin())) == 32


async def test_ingest_moves_and_defers(
    app: FastAPI, paths, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    src = write(tmp_path / "in" / "photo.png", png_bytes((5, 50, 5)))
    ticket = await ingest(src, IngestOrigin(source_key="site", job_run_id=12), paths=paths)

    assert not src.exists()
    assert ticket.batch_id == "run-12"
    assert ticket.staging_path == "staging/run-12/photo.png"
    assert paths.resolve(ticket.staging_path).is_file()
    job = db_conn.execute(
        "SELECT task_name, args->>'batch_id' FROM procrastinate_jobs WHERE id = %s",
        (ticket.job_id,),
    ).fetchone()
    assert job == ("process_ingest", "run-12")

    # A second file with the same name in the batch gets a distinct staging name.
    src2 = write(tmp_path / "in" / "photo.png", png_bytes((6, 60, 6)))
    second = await ingest(src2, IngestOrigin(job_run_id=12), paths=paths)
    assert second.staging_path != ticket.staging_path

    await run_queued_jobs()
    rows = db_conn.execute("SELECT outcome::text FROM ingest_events").fetchall()
    assert rows == [("new",), ("new",)]


async def test_cli_import(
    app: FastAPI, db_conn: psycopg.Connection, migrated_db: str, tmp_path: Path, capsys
) -> None:
    root = tmp_path / "collection"
    write(root / "a.png", png_bytes((100, 0, 0)))
    write(root / "b.txt", b"notes")
    write(root / ".hidden.png", png_bytes((0, 100, 0)))
    make_video(root / "sub" / "clip.mp4")
    make_audio(root / "sub" / "tone.mp3")
    write(root / "sub" / "dup.png", png_bytes((100, 0, 0)))

    batch_id, count = await cli.import_directory(
        root, tags=["imported"], force=False, recursive=True, database_url=migrated_db
    )
    assert count == 5
    assert (root / "a.png").exists()  # sources are copied, never moved
    await run_queued_jobs()

    kinds = db_conn.execute(
        "SELECT media_kind::text, count(*) FROM files GROUP BY 1 ORDER BY 1"
    ).fetchall()
    assert kinds == [("audio", 1), ("image", 1), ("other", 1), ("video", 1)]
    outcomes = db_conn.execute(
        "SELECT outcome::text, count(*) FROM ingest_events WHERE batch_id = %s"
        " GROUP BY 1 ORDER BY 1",
        (batch_id,),
    ).fetchall()
    assert outcomes == [("duplicate_exact", 1), ("new", 4)]
    sources = db_conn.execute("SELECT DISTINCT source_key FROM file_sources").fetchall()
    assert sources == [("upload",)]

    _, flat = await cli.import_directory(
        root, tags=[], force=False, recursive=False, database_url=migrated_db
    )
    assert flat == 2


def test_cli_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(["import", str(tmp_path / "missing")])
