"""Tag listing, autocomplete, rename with merge, delete and per-file editing (section 9)."""

import psycopg
import pytest

from app.library import tags
from tests.library.helpers import fake_archived, make_folder, set_tag, tag_id, tag_rows, tx
from tests.review.helpers import inbox

pytestmark = pytest.mark.usefixtures("clean_ingest")


def files(conn: psycopg.Connection, n: int) -> list[int]:
    return fake_archived(conn, inbox(conn), n)


async def test_list_counts_active_rows_only(sessionmaker, db_conn) -> None:
    a, b, c = files(db_conn, 3)
    set_tag(db_conn, a, "cat", "user")
    set_tag(db_conn, b, "cat", "crawler")
    set_tag(db_conn, c, "cat", "crawler", removed=True)
    set_tag(db_conn, a, "unused_tag", "user", removed=True)
    db_conn.execute("INSERT INTO tags (name) VALUES ('lonely')")

    async with sessionmaker() as session:
        listed = [(t.name, t.count) for t in await tags.list_tags(session)]
        filtered = [t.name for t in await tags.list_tags(session, "Unused Tag")]
        percent = await tags.list_tags(session, "%")
    assert listed == [("cat", 2), ("lonely", 0), ("unused_tag", 0)]
    assert filtered == ["unused_tag"]
    assert percent == []


async def test_autocomplete_orders_by_usage_then_name(sessionmaker, db_conn) -> None:
    ids = files(db_conn, 4)
    for f in ids[:3]:
        set_tag(db_conn, f, "cat_toy", "user")
    for f in ids[:2]:
        set_tag(db_conn, f, "cat", "crawler")
    set_tag(db_conn, ids[0], "catalog", "user")
    set_tag(db_conn, ids[1], "car", "user")
    for f in ids:  # removed rows do not count
        set_tag(db_conn, f, "cathedral", "crawler", removed=True)
    db_conn.execute(
        "INSERT INTO tags (name) VALUES ('cat_a'), ('cab'), ('cat%'), ('artist:cat'), ('catx')"
    )

    async with sessionmaker() as session:
        found = [(t.name, t.count) for t in await tags.autocomplete(session, "Cat") if t.count]
        everything = [t.name for t in await tags.autocomplete(session, "Cat")]
        unused = set(everything[3:])
        percent = [t.name for t in await tags.autocomplete(session, "cat%")]
        two = [t.name for t in await tags.autocomplete(session, "cat", limit=2)]
        underscore = [t.name for t in await tags.autocomplete(session, "cat_")]
        spaced = [t.name for t in await tags.autocomplete(session, "cat t")]
        trailing_space = [t.name for t in await tags.autocomplete(session, "cat ")]
        namespace = [t.name for t in await tags.autocomplete(session, "Artist:")]
        empty = await tags.autocomplete(session, "  ")
    assert found == [
        ("cat_toy", 3),
        ("cat", 2),
        ("catalog", 1),
    ]
    # Unused tags come last (their order among themselves follows the collation).
    assert unused == {"cat%", "cat_a", "cathedral", "catx"}
    assert percent == ["cat%"]  # "%" is literal, not a LIKE wildcard
    assert two == ["cat_toy", "cat"]
    assert underscore == ["cat_toy", "cat_a"]  # "_" is literal too
    assert spaced == ["cat_toy"]  # the prefix is normalized
    assert trailing_space == underscore
    assert namespace == ["artist:cat"]
    assert empty == []


async def test_rename_without_clash(sessionmaker, db_conn) -> None:
    (a,) = files(db_conn, 1)
    set_tag(db_conn, a, "cats", "crawler")
    old = tag_id(db_conn, "cats")

    result = await tx(sessionmaker, tags.rename_tag, old, "  Feline Friends ")

    assert result.merged_from is None and result.tag.name == "feline_friends"
    assert tag_id(db_conn, "feline_friends") == old
    assert tag_rows(db_conn, a) == {"feline_friends": ("crawler", False)}
    with pytest.raises(tags.TagError):
        await tx(sessionmaker, tags.rename_tag, old, " _ ")


async def test_rename_into_existing_tag_merges_rows(sessionmaker, db_conn) -> None:
    ids = files(db_conn, 8)
    # (rows of "cats", rows of "cat") per file -> expected merged row on "cat"
    cases = [
        (("crawler", False), None, ("crawler", False)),  # only cats: re-pointed
        (None, ("user", False), ("user", False)),  # only cat: unchanged
        (("crawler", False), ("crawler", False), ("crawler", False)),
        (("user", False), ("crawler", False), ("user", False)),  # user beats crawler
        (("crawler", False), ("user", False), ("user", False)),
        (("user", False), ("crawler", True), ("user", False)),  # the active row wins
        (("crawler", True), ("user", False), ("user", False)),
        (("user", True), ("crawler", True), ("user", True)),  # both removed: stays removed
    ]
    for f, (src, dst, _) in zip(ids, cases, strict=True):
        if src:
            set_tag(db_conn, f, "cats", src[0], src[1])
        if dst:
            set_tag(db_conn, f, "cat", dst[0], dst[1])
    cat = tag_id(db_conn, "cat")
    cats = tag_id(db_conn, "cats")

    result = await tx(sessionmaker, tags.rename_tag, cats, "Cat")

    assert result.merged_from == "cats"
    assert result.tag.id == cat and result.tag.name == "cat"
    assert result.tag.count == 7
    for f, (_, _, expected) in zip(ids, cases, strict=True):
        assert tag_rows(db_conn, f) == {"cat": expected}, f
    assert db_conn.execute("SELECT count(*) FROM tags WHERE name = 'cats'").fetchone() == (0,)


async def test_delete_requires_force_when_in_use(sessionmaker, db_conn) -> None:
    a, b = files(db_conn, 2)
    set_tag(db_conn, a, "used", "user")
    set_tag(db_conn, b, "only_removed", "crawler", removed=True)
    used, only_removed = tag_id(db_conn, "used"), tag_id(db_conn, "only_removed")

    with pytest.raises(tags.TagInUse, match="used by 1 file"):
        await tx(sessionmaker, tags.delete_tag, used)
    await tx(sessionmaker, tags.delete_tag, only_removed)
    deleted = await tx(sessionmaker, tags.delete_tag, used, force=True)

    assert deleted.name == "used"
    assert tag_rows(db_conn, a) == {} and tag_rows(db_conn, b) == {}
    with pytest.raises(tags.TagNotFound):
        await tx(sessionmaker, tags.delete_tag, used)


async def test_per_file_add_remove_restore(sessionmaker, db_conn) -> None:
    (a,) = files(db_conn, 1)
    set_tag(db_conn, a, "crawled", "crawler")
    set_tag(db_conn, a, "gone", "crawler", removed=True)

    added = await tx(sessionmaker, tags.add_user_tag, a, " New  Tag ")
    assert added.name == "new_tag"
    await tx(sessionmaker, tags.add_user_tag, a, "gone")  # removed row: active, origin user
    await tx(sessionmaker, tags.add_user_tag, a, "CRAWLED")  # active row: unchanged
    assert tag_rows(db_conn, a) == {
        "crawled": ("crawler", False),
        "gone": ("user", False),
        "new_tag": ("user", False),
    }

    assert await tx(sessionmaker, tags.remove_tag, a, tag_id(db_conn, "crawled"))
    assert await tx(sessionmaker, tags.remove_tag, a, tag_id(db_conn, "new_tag"))
    assert tag_rows(db_conn, a)["crawled"] == ("crawler", True)
    assert tag_rows(db_conn, a)["new_tag"] == ("user", True)

    assert await tx(sessionmaker, tags.restore_tag, a, tag_id(db_conn, "crawled"))
    assert tag_rows(db_conn, a)["crawled"] == ("crawler", False)  # restore keeps the origin

    make_folder(db_conn, "unrelated")
    db_conn.execute("INSERT INTO tags (name) VALUES ('elsewhere')")
    assert not await tx(sessionmaker, tags.remove_tag, a, tag_id(db_conn, "elsewhere"))
    with pytest.raises(tags.TagError):
        await tx(sessionmaker, tags.add_user_tag, a, " : ")
