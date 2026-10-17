import os
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg

from tests.ingest.helpers import one


def make_folder(conn: psycopg.Connection, name: str, parent: int | None = None) -> int:
    return one(
        conn, "INSERT INTO folders (parent_id, name) VALUES (%s, %s) RETURNING id", (parent, name)
    )[0]


def fake_archived(conn: psycopg.Connection, folder_id: int, n: int, prefix: str = "f") -> list[int]:
    """Archived files rows without originals on disk (enough for folder and tag queries)."""
    ids = []
    for i in range(n):
        ids.append(
            one(
                conn,
                "INSERT INTO files (sha256, size_bytes, media_kind, status, folder_id,"
                " original_filename, archived_at, reviewed_at)"
                " VALUES (%s, %s, 'image', 'archived', %s, %s, now(), now()) RETURNING id",
                (os.urandom(32), 100 + i, folder_id, f"{prefix}{i}.png"),
            )[0]
        )
    return ids


def folder_of(conn: psycopg.Connection, file_id: int) -> int | None:
    return one(conn, "SELECT folder_id FROM files WHERE id = %s", (file_id,))[0]


def tag_rows(conn: psycopg.Connection, file_id: int) -> dict[str, tuple[str, bool]]:
    """name -> (origin, removed) for every tag row of a file."""
    rows = conn.execute(
        "SELECT t.name, ft.origin::text, ft.removed FROM file_tags ft"
        " JOIN tags t ON t.id = ft.tag_id WHERE ft.file_id = %s",
        (file_id,),
    ).fetchall()
    return {name: (origin, removed) for name, origin, removed in rows}


def tag_id(conn: psycopg.Connection, name: str) -> int:
    return one(conn, "SELECT id FROM tags WHERE name = %s", (name,))[0]


def set_tag(
    conn: psycopg.Connection, file_id: int, name: str, origin: str, removed: bool = False
) -> None:
    conn.execute("INSERT INTO tags (name) VALUES (%s) ON CONFLICT DO NOTHING", (name,))
    conn.execute(
        "INSERT INTO file_tags (file_id, tag_id, origin, removed)"
        " VALUES (%s, (SELECT id FROM tags WHERE name = %s), %s, %s)",
        (file_id, name, origin, removed),
    )


async def tx(sessionmaker, fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    """Run one service call in its own transaction and commit it."""
    async with sessionmaker() as session:
        result = await fn(session, *args, **kwargs)
        await session.commit()
    return result
