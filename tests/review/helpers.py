from collections.abc import Awaitable, Callable

import psycopg

from app.review.service import ReviewResult
from app.storage import DataPaths
from tests.ingest.helpers import one, run
from tests.media import structured_png


async def pending(sessionmaker, paths: DataPaths, conn: psycopg.Connection, n: int, seed=0):
    """Ingest n distinct structured images; return their ids (pending_review)."""
    ids = []
    for i in range(n):
        name = f"p{seed + i}.png"
        await run(sessionmaker, paths, structured_png(seed + i), name)
        ids.append(one(conn, "SELECT id FROM files WHERE original_filename = %s", (name,))[0])
    return ids


async def decide(
    sessionmaker, fn: Callable[..., Awaitable[ReviewResult]], *args, **kwargs
) -> ReviewResult:
    """Run one review operation in its own transaction and commit it."""
    async with sessionmaker() as session:
        result = await fn(session, *args, **kwargs)
        await session.commit()
    return result


def file_row(conn: psycopg.Connection, file_id: int) -> dict:
    cur = conn.execute(
        "SELECT status::text, folder_id, staging_path, reviewed_at, archived_at, discarded_at,"
        " encode(sha256, 'hex'), ext, has_thumb, phash FROM files WHERE id = %s",
        (file_id,),
    )
    row = cur.fetchone()
    assert row is not None
    keys = [
        "status", "folder_id", "staging_path", "reviewed_at", "archived_at", "discarded_at",
        "sha", "ext", "has_thumb", "phash",
    ]  # fmt: skip
    return dict(zip(keys, row, strict=True))


def inbox(conn: psycopg.Connection) -> int:
    return one(conn, "SELECT id FROM folders WHERE parent_id IS NULL AND name = 'Inbox'")[0]


def active_tags(conn: psycopg.Connection, file_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT t.name, ft.origin::text FROM file_tags ft JOIN tags t ON t.id = ft.tag_id"
        " WHERE ft.file_id = %s AND NOT ft.removed",
        (file_id,),
    ).fetchall()
    return dict(rows)


def locations(paths: DataPaths, row: dict) -> dict[str, bool]:
    """Where the original of a file is on disk."""
    sha, ext = row["sha"], row["ext"]
    staged = bool(row["staging_path"]) and paths.resolve(row["staging_path"]).is_file()
    return {
        "staging": staged,
        "archive": paths.archive_path(sha, ext).is_file(),
        "trash": paths.trash_path(sha, ext).is_file(),
    }
