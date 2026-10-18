import os
import time
import uuid

import psycopg
import pytest

from app.jobs import run_queued_jobs
from app.jobs.tasks import integrity as integrity_task
from app.ops import integrity
from app.review import service
from tests.media import structured_png, write
from tests.review.helpers import decide, file_row, pending

pytestmark = pytest.mark.usefixtures("clean_ingest")

PROBLEM_KINDS = list(integrity.PROBLEMS)


def report(conn: psycopg.Connection, run_id: int | None = None) -> tuple[int, dict]:
    sql = "SELECT id, stats FROM job_runs WHERE kind = 'integrity'"
    if run_id is not None:
        sql += f" AND id = {int(run_id)}"
    row = conn.execute(sql + " ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    return row[0], row[1]


def age(path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


async def scenario(sessionmaker, paths, conn) -> dict:
    """One file per problem kind, plus files that must not be reported."""
    ids = await pending(sessionmaker, paths, conn, 8)
    missing_arch, modified, healthy, no_thumb = ids[:4]
    missing_stage, recent_discard, old_discard, _fine_pending = ids[4:]
    await decide(sessionmaker, service.archive, paths, [missing_arch, modified, healthy, no_thumb])
    await decide(sessionmaker, service.discard, paths, [recent_discard, old_discard])
    conn.execute(
        "UPDATE files SET discarded_at = now() - interval '30 days' WHERE id = %s", (old_discard,)
    )
    rows = {i: file_row(conn, i) for i in ids}

    paths.archive_path(rows[missing_arch]["sha"], "png").unlink()
    target = paths.archive_path(rows[modified]["sha"], "png")
    target.write_bytes(target.read_bytes() + b"tampered")
    paths.thumb_path(rows[no_thumb]["sha"]).unlink()
    paths.resolve(rows[missing_stage]["staging_path"]).unlink()
    paths.trash_path(rows[recent_discard]["sha"], "png").unlink()
    paths.trash_path(rows[old_discard]["sha"], "png").unlink()  # past retention: fine

    orphan_archive = write(paths.archive / "ff" / "ee" / ("ffee" + "0" * 60 + ".jpg"), b"x")
    batch = paths.staging_batch(uuid.uuid4().hex)
    orphan_staging = write(batch / "orphan.png", structured_png(77))
    age(orphan_staging, 7200)
    write(batch / "fresh.png", structured_png(78))  # may still be on its way in
    partial = write(batch / ".upload-abc.part", b"half")
    age(partial, 7200)
    leftover = paths.work / "999"
    (leftover / "sub").mkdir(parents=True)
    write(leftover / "sub" / "file.bin", b"1")
    running_id = conn.execute(
        "INSERT INTO job_runs (kind, status) VALUES ('rehash', 'running') RETURNING id"
    ).fetchone()[0]
    (paths.work / str(running_id)).mkdir()
    return {
        "ids": rows,
        "missing_arch": missing_arch,
        "modified": modified,
        "no_thumb": no_thumb,
        "missing_stage": missing_stage,
        "recent_discard": recent_discard,
        "orphan_archive": orphan_archive,
        "orphan_staging": orphan_staging,
        "partial": partial,
        "leftover": leftover,
        "running_work": paths.work / str(running_id),
        "running_id": running_id,
    }


async def test_each_problem_type_is_detected(sessionmaker, paths, db_conn) -> None:
    s = await scenario(sessionmaker, paths, db_conn)
    assert await integrity_task(job_run_id=None) == "succeeded"
    _, stats = report(db_conn)
    rel = lambda p: p.relative_to(paths.root).as_posix()  # noqa: E731

    assert [e["file_id"] for e in stats["problems"]["missing_archive"]] == [s["missing_arch"]]
    assert [e["file_id"] for e in stats["problems"]["missing_staging"]] == [s["missing_stage"]]
    assert [e["file_id"] for e in stats["problems"]["missing_trash"]] == [s["recent_discard"]]
    assert [e["path"] for e in stats["problems"]["orphan_archive"]] == [rel(s["orphan_archive"])]
    staging = {e["path"]: e["partial"] for e in stats["problems"]["orphan_staging"]}
    assert staging == {rel(s["orphan_staging"]): False, rel(s["partial"]): True}
    assert [e["name"] for e in stats["problems"]["leftover_work"]] == ["999"]
    assert [e["file_id"] for e in stats["problems"]["missing_thumb"]] == [s["no_thumb"]]
    assert stats["problems"]["hash_mismatch"] == []  # not verified without the option
    assert stats["verify_hashes"] is False
    assert stats["counts"] == {
        "missing_archive": 1,
        "missing_staging": 1,
        "missing_trash": 1,
        "orphan_archive": 1,
        "orphan_staging": 2,
        "leftover_work": 1,
        "missing_thumb": 1,
        "hash_mismatch": 0,
    }
    assert stats["checked"]["files"] == 8


async def test_verify_hashes_catches_a_modified_file(sessionmaker, paths, db_conn) -> None:
    s = await scenario(sessionmaker, paths, db_conn)
    assert await integrity_task(job_run_id=None, verify_hashes=True) == "succeeded"
    _, stats = report(db_conn)
    mismatches = stats["problems"]["hash_mismatch"]
    assert [e["file_id"] for e in mismatches] == [s["modified"]]
    assert mismatches[0]["expected"] == s["ids"][s["modified"]]["sha"]
    assert mismatches[0]["actual"] != mismatches[0]["expected"]
    assert stats["checked"]["hashed"] == 3  # the three archive files still present


async def test_one_deleted_archive_file_is_exactly_what_is_reported(
    sessionmaker, paths, db_conn
) -> None:
    ids = await pending(sessionmaker, paths, db_conn, 3)
    await decide(sessionmaker, service.archive, paths, ids)
    victim = file_row(db_conn, ids[1])
    paths.archive_path(victim["sha"], "png").unlink()
    await integrity_task(job_run_id=None, verify_hashes=True)
    _, stats = report(db_conn)
    assert stats["total_problems"] == 1
    assert stats["problems"]["missing_archive"] == [
        {
            "file_id": ids[1],
            "path": paths.archive_path(victim["sha"], "png").relative_to(paths.root).as_posix(),
        }
    ]


async def test_fixes_do_only_what_they_claim(client, sessionmaker, paths, db_conn) -> None:
    s = await scenario(sessionmaker, paths, db_conn)
    await integrity_task(job_run_id=None, verify_hashes=True)
    run_id, _ = report(db_conn)
    snapshot = sorted(p.relative_to(paths.root).as_posix() for p in paths.root.rglob("*"))
    page = await client.get(f"/jobs/{run_id}")
    assert 'id="integrity-report"' in page.text
    for action in ("thumbs", "staging", "work"):
        assert f'data-fix="{action}"' in page.text
    assert 'data-count="missing_archive">1<' in page.text

    # Thumbnails: only the missing one is written, nothing else changes.
    thumb = paths.thumb_path(s["ids"][s["no_thumb"]]["sha"])
    r = await client.post(f"/jobs/{run_id}/fix/thumbs")
    assert r.status_code == 303
    assert thumb.is_file()
    after = sorted(p.relative_to(paths.root).as_posix() for p in paths.root.rglob("*"))
    assert set(after) - set(snapshot) == {thumb.relative_to(paths.root).as_posix()}
    assert set(snapshot) - set(after) == set()

    # Work directories: the leftover goes, the running run's directory stays.
    r = await client.post(f"/jobs/{run_id}/fix/work")
    assert not s["leftover"].exists() and s["running_work"].is_dir()

    # Orphan staging files are ingested; the partial upload is left alone.
    r = await client.post(f"/jobs/{run_id}/fix/staging")
    assert not s["orphan_staging"].exists() and s["partial"].exists()
    await run_queued_jobs()
    new = db_conn.execute(
        "SELECT status::text FROM files WHERE original_filename = 'orphan.png'"
    ).fetchall()
    assert new == [("pending_review",)]

    # Reported-only problems are untouched.
    assert s["orphan_archive"].is_file()
    assert (
        paths.archive_path(s["ids"][s["modified"]]["sha"], "png").read_bytes().endswith(b"tampered")
    )
    assert not paths.archive_path(s["ids"][s["missing_arch"]]["sha"], "png").exists()
    statuses = dict(db_conn.execute("SELECT id, status::text FROM files").fetchall())
    assert (
        statuses[s["missing_arch"]] == "archived"
        and statuses[s["missing_stage"]] == "pending_review"
    )

    _, stats = report(db_conn, run_id)
    assert stats["fixes"]["thumbs"]["regenerated"] == 1
    assert stats["fixes"]["work"]["deleted"] == 1
    assert stats["fixes"]["staging"]["ingested"] == 1
    page = await client.get(f"/jobs/{run_id}")
    assert 'data-fix-result="staging"' in page.text

    # A fresh check after the fixes no longer lists them.
    await integrity_task(job_run_id=None)
    _, stats = report(db_conn)
    assert stats["counts"]["missing_thumb"] == 0
    assert stats["counts"]["leftover_work"] == 0
    assert [e["partial"] for e in stats["problems"]["orphan_staging"]] == [True]
    assert stats["counts"]["orphan_archive"] == 1


async def test_fix_needs_a_finished_integrity_run(client, db_conn) -> None:
    other = db_conn.execute(
        "INSERT INTO job_runs (kind, status) VALUES ('backup', 'succeeded') RETURNING id"
    ).fetchone()[0]
    assert (await client.post(f"/jobs/{other}/fix/thumbs")).status_code == 404
    assert (await client.post("/jobs/999999/fix/thumbs")).status_code == 404
    assert (await client.post(f"/jobs/{other}/fix/archive")).status_code == 404


async def test_integrity_from_settings_records_a_run(client, db_conn) -> None:
    r = await client.post("/settings/run/integrity", data={"verify_hashes": "1"})
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    await run_queued_jobs()
    row = db_conn.execute(
        "SELECT status::text, stats->'verify_hashes', stats->'options' FROM job_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    assert row == ("succeeded", True, {"verify_hashes": True})
