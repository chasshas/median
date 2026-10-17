"""Search speed on a generated collection of 50,000 files and 500 tags (marked slow)."""

import time
from datetime import date

import pytest

from app.search.filters import SearchFilter, search
from app.search.query import parse
from tests.review.helpers import inbox

pytestmark = [pytest.mark.slow, pytest.mark.usefixtures("clean_ingest")]

FILES = 50_000
TAGS = 500


def generate(conn) -> int:
    """Folders (10 top level x 10 children), files spread over them and one year of dates,
    and 1-8 tags per file with a skewed popularity (some tags are on many files)."""
    box = inbox(conn)
    conn.execute(
        "INSERT INTO folders (parent_id, name)"
        " SELECT NULL, 'Top ' || i FROM generate_series(1, 10) i"
    )
    conn.execute(
        "INSERT INTO folders (parent_id, name)"
        " SELECT f.id, 'Child ' || i FROM folders f, generate_series(1, 10) i"
        " WHERE f.parent_id IS NULL AND f.name LIKE 'Top %'"
    )
    conn.execute(
        "INSERT INTO tags (name) SELECT 'tag' || i FROM generate_series(0, %s - 1) i", (TAGS,)
    )
    conn.execute(
        "INSERT INTO files (sha256, size_bytes, media_kind, status, folder_id, original_filename,"
        " collected_at, archived_at)"
        " SELECT sha256(int8send(i)), 1000 + i %% 977,"
        "  (ARRAY['image','image','image','video','audio'])[1 + i %% 5]::media_kind,"
        "  (CASE WHEN i %% 10 = 0 THEN 'discarded' ELSE 'archived' END)::file_status,"
        "  (SELECT id FROM folders ORDER BY id OFFSET (i %% 111) LIMIT 1),"
        "  'file' || i || '.jpg',"
        "  timestamptz '2026-01-01 00:00+09' + (i %% 365) * interval '1 day'"
        "   + (i %% 86400) * interval '1 second',"
        "  timestamptz '2026-01-01 00:00+09' + (i %% 365) * interval '1 day' + interval '2 days'"
        " FROM generate_series(1, %s) i",
        (FILES,),
    )
    conn.execute("SELECT setseed(0.42)")
    conn.execute(
        "INSERT INTO file_tags (file_id, tag_id, origin, removed)"
        " SELECT DISTINCT ON (f.id, t.id) f.id, t.id, 'crawler', random() < 0.02"
        " FROM files f CROSS JOIN LATERAL ("
        "  SELECT floor(%s * power(random(), 2))::int + 0 * f.id AS n"
        "  FROM generate_series(1, 1 + f.id %% 8)"
        " ) r JOIN tags t ON t.name = 'tag' || r.n",
        (TAGS,),
    )
    conn.execute("ANALYZE files, file_tags, tags, folders")
    return box


async def test_first_page_under_300_ms(sessionmaker, db_conn) -> None:
    generate(db_conn)
    assert db_conn.execute("SELECT count(*) FROM files").fetchone()[0] == FILES
    assert db_conn.execute("SELECT count(*) FROM tags").fetchone()[0] == TAGS
    query = 'tag1 tag3 -tag2 folder:"Top 2" collected:2026-02-01..2026-11-30'
    try:
        async with sessionmaker() as session:
            await search(session, SearchFilter(tags_all=["tag0"]))  # warm the connection
            started = time.perf_counter()
            parsed = await parse(session, query)
            page = await search(session, parsed.filter)
            elapsed = time.perf_counter() - started
        flt = parsed.filter
        assert flt.tags_all == ["tag1", "tag3"] and flt.tags_none == ["tag2"]
        assert flt.folder_id is not None and flt.include_subfolders
        assert flt.collected_from == date(2026, 2, 1)
        assert page.total > 0 and page.files, "the query should match some files"
        assert elapsed < 0.3, f"first page took {elapsed * 1000:.0f} ms"
    finally:
        db_conn.execute("TRUNCATE file_tags, tags, files RESTART IDENTITY CASCADE")
        db_conn.execute("DELETE FROM folders WHERE NOT (parent_id IS NULL AND name = 'Inbox')")
