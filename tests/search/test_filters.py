"""The query builder: each filter alone and combined, date boundaries, keyset pagination."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.db.models_files import MediaKind
from app.search.filters import SORTS, CursorError, SearchFilter, day_bounds, search
from tests.library.helpers import make_folder, set_tag
from tests.review.helpers import inbox
from tests.search.helpers import add_file, add_source

pytestmark = pytest.mark.usefixtures("clean_ingest")


async def ids(sessionmaker, **kwargs) -> list[int]:
    async with sessionmaker() as session:
        page = await search(session, SearchFilter(**kwargs), limit=500)
    assert page.total == len(page.files)
    return sorted(f.id for f in page.files)


async def test_tags_folders_and_combined(sessionmaker, db_conn) -> None:
    animals = make_folder(db_conn, "Animals")
    cats = make_folder(db_conn, "Cats", animals)
    other = make_folder(db_conn, "Other")
    a = add_file(db_conn, folder_id=animals)  # cat
    b = add_file(db_conn, folder_id=cats)  # cat, dog
    c = add_file(db_conn, folder_id=cats)  # dog
    d = add_file(db_conn, folder_id=other)  # cat (removed), red
    e = add_file(db_conn, status="pending_review")  # cat
    set_tag(db_conn, a, "cat", "user")
    set_tag(db_conn, b, "cat", "crawler")
    set_tag(db_conn, b, "dog", "user")
    set_tag(db_conn, c, "dog", "crawler")
    set_tag(db_conn, d, "cat", "user", removed=True)
    set_tag(db_conn, d, "red", "user")
    set_tag(db_conn, e, "cat", "user")

    assert await ids(sessionmaker) == [a, b, c, d]  # archived by default
    assert await ids(sessionmaker, tags_all=["cat"]) == [a, b]  # removed rows never match
    assert await ids(sessionmaker, tags_all=["cat", "dog"]) == [b]
    assert await ids(sessionmaker, tags_all=["cat", "ghost"]) == []
    assert await ids(sessionmaker, tags_any=["dog", "red"]) == [b, c, d]
    assert await ids(sessionmaker, tags_any=["ghost"]) == []
    assert await ids(sessionmaker, tags_any=["ghost", "red"]) == [d]
    assert await ids(sessionmaker, tags_none=["cat"]) == [c, d]
    assert await ids(sessionmaker, tags_none=["ghost"]) == [a, b, c, d]
    assert await ids(sessionmaker, folder_id=animals) == [a, b, c]  # subtree
    assert await ids(sessionmaker, folder_id=animals, include_subfolders=False) == [a]
    assert await ids(sessionmaker, folder_id=cats) == [b, c]
    assert await ids(sessionmaker, tags_all=["cat"], tags_none=["dog"], folder_id=animals) == [a]
    assert await ids(sessionmaker, tags_all=["cat"], statuses=["pending_review"]) == [e]
    assert await ids(sessionmaker, tags_all=["cat"], statuses=["archived", "pending_review"]) == [
        a,
        b,
        e,
    ]


async def test_type_source_status_and_text(sessionmaker, db_conn) -> None:
    box = inbox(db_conn)
    img = add_file(db_conn, folder_id=box, name="Sunset.PNG", title="Evening")
    vid = add_file(db_conn, folder_id=box, media_kind="video", description="a 100% real clip")
    aud = add_file(db_conn, folder_id=box, media_kind="audio", name="song_1.mp3")
    gone = add_file(db_conn, status="discarded", name="sunset copy.png")
    add_source(db_conn, img, "upload")
    add_source(db_conn, vid, "ytdlp")
    add_source(db_conn, vid, "upload")

    assert await ids(sessionmaker, media_kinds=[MediaKind.video, MediaKind.audio]) == [vid, aud]
    assert await ids(sessionmaker, source_keys=["upload"]) == [img, vid]
    assert await ids(sessionmaker, source_keys=["ytdlp", "gallerydl"]) == [vid]
    assert await ids(sessionmaker, statuses=["discarded"]) == [gone]
    assert await ids(sessionmaker, text=["sunset"]) == [img]
    assert await ids(sessionmaker, text=["SUNSET"], statuses=["archived", "discarded"]) == [
        img,
        gone,
    ]
    assert await ids(sessionmaker, text=["evening"]) == [img]  # title
    assert await ids(sessionmaker, text=["100%"]) == [vid]  # description, % is literal
    assert await ids(sessionmaker, text=["g_1"]) == [aud]  # _ is literal
    assert await ids(sessionmaker, text=["100_"]) == []
    assert await ids(sessionmaker, text=["sunset", "evening"]) == [img]
    assert await ids(sessionmaker, text=["sunset", "clip"]) == []


def test_day_bounds_in_seoul() -> None:
    lower, upper = day_bounds(date(2026, 1, 1), date(2026, 1, 31), "Asia/Seoul")
    assert lower == datetime(2025, 12, 31, 15, tzinfo=UTC)
    assert upper == datetime(2026, 1, 31, 15, tzinfo=UTC)
    assert day_bounds(None, None, "Asia/Seoul") == (None, None)


async def test_date_boundaries_at_local_midnight(sessionmaker, db_conn) -> None:
    # Asia/Seoul is UTC+9: local midnight of 2026-01-01 is 2025-12-31 15:00 UTC.
    midnight = datetime(2025, 12, 31, 15, tzinfo=UTC)
    before = add_file(db_conn, collected_at=midnight - timedelta(microseconds=1))
    at = add_file(db_conn, collected_at=midnight)
    last = add_file(db_conn, collected_at=midnight + timedelta(days=1) - timedelta(microseconds=1))
    next_day = add_file(db_conn, collected_at=midnight + timedelta(days=1))

    day = date(2026, 1, 1)
    assert await ids(sessionmaker, collected_from=day, collected_to=day) == [at, last]
    assert await ids(sessionmaker, collected_from=day) == [at, last, next_day]
    assert await ids(sessionmaker, collected_to=day) == [before, at, last]
    assert await ids(sessionmaker, collected_to=date(2025, 12, 31)) == [before]

    archived = add_file(db_conn, archived_at=midnight + timedelta(days=31))  # 2026-02-01 KST
    pending = add_file(db_conn, status="pending_review")
    feb = date(2026, 2, 1)
    assert await ids(sessionmaker, archived_from=feb, archived_to=feb) == [archived]
    assert archived not in await ids(sessionmaker, archived_from=feb + timedelta(days=1))
    assert archived not in await ids(sessionmaker, archived_to=feb - timedelta(days=1))
    # Files without archived_at never match an archived range.
    both = ["archived", "pending_review"]
    assert pending not in await ids(sessionmaker, archived_to=date(2100, 1, 1), statuses=both)


async def all_pages(sessionmaker, flt: SearchFilter, limit: int) -> list[int]:
    out: list[int] = []
    cursor = None
    async with sessionmaker() as session:
        while True:
            page = await search(session, flt, cursor, limit)
            out += [f.id for f in page.files]
            assert page.total == 23
            if page.next_cursor is None:
                return out
            cursor = page.next_cursor


@pytest.mark.parametrize("sort", SORTS)
async def test_pagination_without_duplicates_or_gaps(sessionmaker, db_conn, sort: str) -> None:
    base = datetime(2026, 3, 1, tzinfo=UTC)
    created = []
    for i in range(20):
        # Repeated sizes and timestamps: ties are broken by id.
        created.append(
            add_file(
                db_conn,
                size=100 + i % 3,
                collected_at=base + timedelta(hours=i % 4),
                archived_at=base + timedelta(days=i % 5),
            )
        )
    statuses = ["archived", "pending_review"]
    # Three pending files have no archived_at: they come last in both archived orders.
    nulls = [add_file(db_conn, status="pending_review") for _ in range(3)]
    flt = SearchFilter(statuses=statuses, sort=sort, seed=42 if sort == "random" else None)
    expected = await all_pages(sessionmaker, flt, 500)
    assert sorted(expected) == sorted(created + nulls)
    for limit in (1, 4, 7):
        assert await all_pages(sessionmaker, flt, limit) == expected
    if sort.startswith("archived"):
        assert sorted(expected[-3:]) == nulls
    if sort == "size_asc":
        assert expected[:7] == sorted(i for i in created if (i - created[0]) % 3 == 0)[:7]
    if sort == "random":
        # A fixed seed gives a fixed order; another seed (almost surely) another one.
        other = SearchFilter(statuses=statuses, sort="random", seed=7)
        assert await all_pages(sessionmaker, other, 5) != expected


async def test_random_without_seed_gets_one(sessionmaker, db_conn) -> None:
    add_file(db_conn)
    async with sessionmaker() as session:
        page = await search(session, SearchFilter(sort="random"))
    assert page.filter.seed is not None


async def test_bad_cursors(sessionmaker, db_conn) -> None:
    for _ in range(3):
        add_file(db_conn)
    async with sessionmaker() as session:
        page = await search(session, SearchFilter(), limit=1)
        assert page.next_cursor
        with pytest.raises(CursorError):
            await search(session, SearchFilter(sort="size_asc"), page.next_cursor)
        with pytest.raises(CursorError):
            await search(session, SearchFilter(), "not a cursor!")
