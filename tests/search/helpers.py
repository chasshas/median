import os
from datetime import UTC, datetime

import psycopg

from tests.ingest.helpers import one


def add_file(
    conn: psycopg.Connection,
    *,
    status: str = "archived",
    folder_id: int | None = None,
    media_kind: str = "image",
    size: int = 100,
    collected_at: datetime | None = None,
    archived_at: datetime | None = None,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
) -> int:
    """A files row without an original on disk (enough for search queries)."""
    now = datetime.now(UTC)
    if status == "archived" and archived_at is None:
        archived_at = now
    return one(
        conn,
        "INSERT INTO files (sha256, size_bytes, media_kind, status, folder_id, original_filename,"
        " title, description, collected_at, archived_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            os.urandom(32),
            size,
            media_kind,
            status,
            folder_id,
            name,
            title,
            description,
            collected_at or now,
            archived_at,
        ),
    )[0]


def add_source(conn: psycopg.Connection, file_id: int, source_key: str) -> None:
    conn.execute(
        "INSERT INTO file_sources (file_id, source_key) VALUES (%s, %s)", (file_id, source_key)
    )
