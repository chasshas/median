import psycopg
import pytest

from app.db.migrate import migrate
from app.jobs.schema import ensure_job_schema


def test_migrate_is_idempotent(migrated_db: str, db_conn: psycopg.Connection) -> None:
    migrate(migrated_db)
    migrate(migrated_db)

    assert ensure_job_schema(migrated_db) is False
    version = db_conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert version == ("0003",)
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
    assert {"similar_pairs", "video_frame_hashes"} <= tables
    assert not tables & {"crawl_sources", "job_runs"}
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
    inbox = db_conn.execute("SELECT parent_id FROM folders WHERE name = 'Inbox'").fetchall()
    assert inbox == [(None,)]
    fks = db_conn.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'file_sources'::regclass"
        " AND contype = 'f'"
    ).fetchall()
    assert fks == [("fk_file_sources_file_id_files",)]


def test_folder_names_unique_among_siblings(db_conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.UniqueViolation):
        db_conn.execute("INSERT INTO folders (parent_id, name) VALUES (NULL, 'inbox')")
