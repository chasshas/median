import procrastinate
import psycopg

from app.config import libpq_url


def ensure_job_schema(database_url: str) -> bool:
    """Apply the Procrastinate schema if it is missing. Returns True when it was applied.

    Idempotent: an existing schema is left untouched. Upgrading Procrastinate to a
    version with new schema migrations must add an Alembic migration that runs them.
    """
    url = libpq_url(database_url)
    with psycopg.connect(url) as conn:
        row = conn.execute("SELECT to_regclass('public.procrastinate_jobs')").fetchone()
        if row is not None and row[0] is not None:
            return False
        conn.execute(procrastinate.schema.SchemaManager.get_schema())
    return True
