import psycopg

from app.db.migrate import migrate
from app.jobs.schema import ensure_job_schema


def test_migrate_is_idempotent(migrated_db: str, db_conn: psycopg.Connection) -> None:
    migrate(migrated_db)
    migrate(migrated_db)

    assert ensure_job_schema(migrated_db) is False
    version = db_conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert version == ("0001",)
    jobs_table = db_conn.execute("SELECT to_regclass('public.procrastinate_jobs')").fetchone()
    assert jobs_table is not None
    assert jobs_table[0] is not None
