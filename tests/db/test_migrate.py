import psycopg
import pytest

from app.db.migrate import migrate
from app.jobs.schema import ensure_job_schema


def test_migrate_is_idempotent(migrated_db: str, db_conn: psycopg.Connection) -> None:
    migrate(migrated_db)
    migrate(migrated_db)

    assert ensure_job_schema(migrated_db) is False
    version = db_conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert version == ("0005",)
    jobs_table = db_conn.execute("SELECT to_regclass('public.procrastinate_jobs')").fetchone()
    assert jobs_table is not None
    assert jobs_table[0] is not None


def test_schema(db_conn: psycopg.Connection) -> None:
    tables = {
        r[0]
        for r in db_conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    }
    assert {"files", "file_sources", "ingest_events", "folders", "tags", "file_tags"} <= tables
    assert {"similar_pairs", "video_frame_hashes", "review_actions"} <= tables
    assert {"crawl_sources", "job_runs"} <= tables
    enums = dict(
        db_conn.execute(
            "SELECT t.typname, array_agg(e.enumlabel::text ORDER BY e.enumsortorder)"
            " FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid GROUP BY t.typname"
        ).fetchall()
    )
    assert enums["media_kind"] == ["image", "video", "audio", "other"]
    assert enums["file_status"] == [
        "processing",
        "pending_review",
        "archived",
        "discarded",
        "error",
    ]
    assert enums["tag_origin"] == ["crawler", "user"]
    assert enums["similar_method"] == ["image_phash", "video_frames"]
    assert enums["review_action_kind"] == ["archive", "discard", "replace"]
    assert enums["job_kind"] == [
        "crawl",
        "rehash",
        "integrity",
        "backup",
        "update_tools",
        "purge_trash",
    ]
    assert enums["job_status"] == ["queued", "running", "succeeded", "failed", "cancelled"]
    inbox = db_conn.execute("SELECT parent_id FROM folders WHERE name = 'Inbox'").fetchall()
    assert inbox == [(None,)]
    fks = db_conn.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'file_sources'::regclass"
        " AND contype = 'f' ORDER BY conname"
    ).fetchall()
    assert fks == [
        ("fk_file_sources_file_id_files",),
        ("fk_file_sources_job_run_id_job_runs",),
        ("fk_file_sources_source_id_crawl_sources",),
    ]
    event_fks = db_conn.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'ingest_events'::regclass"
        " AND contype = 'f' ORDER BY conname"
    ).fetchall()
    assert ("fk_ingest_events_job_run_id_job_runs",) in event_fks


def test_one_active_run_per_source(db_conn: psycopg.Connection) -> None:
    source_id = db_conn.execute(
        "INSERT INTO crawl_sources (name, addon_key) VALUES ('uq', 'demo') RETURNING id"
    ).fetchone()[0]
    try:
        db_conn.execute(
            "INSERT INTO job_runs (source_id, kind, status) VALUES (%s, 'crawl', 'running')",
            (source_id,),
        )
        db_conn.execute(
            "INSERT INTO job_runs (source_id, kind, status) VALUES (%s, 'crawl', 'succeeded')",
            (source_id,),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            db_conn.execute(
                "INSERT INTO job_runs (source_id, kind, status) VALUES (%s, 'crawl', 'queued')",
                (source_id,),
            )
    finally:
        db_conn.execute("DELETE FROM job_runs WHERE source_id = %s", (source_id,))
        db_conn.execute("DELETE FROM crawl_sources WHERE id = %s", (source_id,))


def test_folder_names_unique_among_siblings(db_conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.UniqueViolation):
        db_conn.execute("INSERT INTO folders (parent_id, name) VALUES (NULL, 'inbox')")
