"""Folder service: create, rename, move, delete with merge, tree counts, paths, Inbox rules."""

import psycopg
import pytest

from app.library import folders
from tests.library.helpers import fake_archived, folder_of, make_folder, tx
from tests.review.helpers import inbox

pytestmark = pytest.mark.usefixtures("clean_ingest")


def names_under(conn: psycopg.Connection, parent: int | None) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM folders WHERE parent_id IS NOT DISTINCT FROM %s ORDER BY lower(name)",
        (parent,),
    ).fetchall()
    return [r[0] for r in rows]


def node_by_path(tree: list[folders.FolderNode]) -> dict[str, folders.FolderNode]:
    return {n.path: n for n in folders.flatten(tree)}


async def test_create_rename_and_paths(sessionmaker, db_conn) -> None:
    a = await tx(sessionmaker, folders.create, "  Art  ")
    b = await tx(sessionmaker, folders.create, "Photos", a.id)
    c = await tx(sessionmaker, folders.create, "2026  Trip", b.id)
    assert (a.name, c.name) == ("Art", "2026 Trip")

    async with sessionmaker() as session:
        assert await folders.path_of(session, c.id) == "Art / Photos / 2026 Trip"
        assert await folders.resolve_path(session, "art/PHOTOS/2026 trip") == c.id
        assert await folders.resolve_path(session, " Art / Photos ") == b.id
        assert await folders.resolve_path(session, "Art/Nope") is None
        assert await folders.resolve_path(session, "") is None
        assert await folders.subtree_ids(session, a.id) == [a.id, b.id, c.id]
        assert await folders.subtree_ids(session, c.id) == [c.id]

    await tx(sessionmaker, folders.rename, b.id, "Pictures")
    async with sessionmaker() as session:
        assert await folders.path_of(session, c.id) == "Art / Pictures / 2026 Trip"

    for bad in ("", "   ", "a/b", "x" * 101):
        with pytest.raises(folders.FolderError):
            await tx(sessionmaker, folders.create, bad)
    with pytest.raises(folders.FolderNotFound):
        await tx(sessionmaker, folders.create, "Orphan", 999999)


async def test_sibling_name_clash(sessionmaker, db_conn) -> None:
    a = make_folder(db_conn, "Art")
    b = make_folder(db_conn, "Photos")
    make_folder(db_conn, "Cats", a)
    dogs = make_folder(db_conn, "Dogs", a)
    cats_b = make_folder(db_conn, "cats", b)

    with pytest.raises(folders.FolderError, match="already exists in Art"):
        await tx(sessionmaker, folders.create, "CATS", a)
    with pytest.raises(folders.FolderError, match="already exists"):
        await tx(sessionmaker, folders.create, "photos")  # top level, case-insensitive
    with pytest.raises(folders.FolderError, match="already exists"):
        await tx(sessionmaker, folders.rename, dogs, "cats")
    with pytest.raises(folders.FolderError, match="already exists"):
        await tx(sessionmaker, folders.move, cats_b, a)
    # The same name in another parent is fine, and renaming to a different case of itself too.
    await tx(sessionmaker, folders.create, "Dogs", b)
    await tx(sessionmaker, folders.rename, dogs, "DOGS")
    assert names_under(db_conn, a) == ["Cats", "DOGS"]


async def test_move_rejects_cycles(sessionmaker, db_conn) -> None:
    a = make_folder(db_conn, "A")
    b = make_folder(db_conn, "B", a)
    c = make_folder(db_conn, "C", b)
    other = make_folder(db_conn, "Other")

    for target in (a, b, c):
        with pytest.raises(folders.FolderError, match="into itself or one of its subfolders"):
            await tx(sessionmaker, folders.move, a, target)
    with pytest.raises(folders.FolderError):
        await tx(sessionmaker, folders.move, b, c)

    await tx(sessionmaker, folders.move, b, other)
    async with sessionmaker() as session:
        assert await folders.path_of(session, c) == "Other / B / C"
    await tx(sessionmaker, folders.move, b, None)  # to the top level
    async with sessionmaker() as session:
        assert await folders.path_of(session, c) == "B / C"


async def test_inbox_is_protected(sessionmaker, db_conn) -> None:
    box = inbox(db_conn)
    art = make_folder(db_conn, "Art")
    for call, args in (
        (folders.rename, (box, "Mail")),
        (folders.move, (box, art)),
        (folders.delete, (box, True)),
    ):
        with pytest.raises(folders.FolderError, match="Inbox cannot be"):
            await tx(sessionmaker, call, *args)
    # Subfolders of the Inbox are allowed; another top-level "inbox" is a clash.
    await tx(sessionmaker, folders.create, "Later", box)
    with pytest.raises(folders.FolderError, match="already exists"):
        await tx(sessionmaker, folders.create, "inbox")
    assert db_conn.execute("SELECT name FROM folders WHERE id = %s", (box,)).fetchone() == (
        "Inbox",
    )


async def test_tree_counts(sessionmaker, db_conn) -> None:
    box = inbox(db_conn)
    a = make_folder(db_conn, "A")
    b = make_folder(db_conn, "B", a)
    c = make_folder(db_conn, "C", b)
    d = make_folder(db_conn, "D", a)
    fake_archived(db_conn, box, 2, "i")
    fake_archived(db_conn, a, 1, "a")
    fake_archived(db_conn, b, 3, "b")
    fake_archived(db_conn, c, 4, "c")
    # Files that are not archived never count.
    db_conn.execute("UPDATE files SET status = 'discarded' WHERE original_filename = 'c3.png'")

    async with sessionmaker() as session:
        tree = await folders.get_tree(session)
    assert [n.name for n in tree] == ["Inbox", "A"]
    nodes = node_by_path(tree)
    expected = {
        "Inbox": (2, 2),
        "A": (1, 1 + 3 + 3),
        "A / B": (3, 6),
        "A / B / C": (3, 3),
        "A / D": (0, 0),
    }
    assert {p: (n.direct_count, n.subtree_count) for p, n in nodes.items()} == expected
    assert [c.name for c in nodes["A"].children] == ["B", "D"]
    assert nodes["Inbox"].is_inbox and not nodes["A"].is_inbox
    assert nodes["A / D"].id == d


async def test_delete_empty_and_non_empty(sessionmaker, db_conn) -> None:
    a = make_folder(db_conn, "A")
    empty = make_folder(db_conn, "Empty", a)
    full = make_folder(db_conn, "Full", a)
    fake_archived(db_conn, full, 1)
    with_child = make_folder(db_conn, "Parent", a)
    make_folder(db_conn, "Child", with_child)

    await tx(sessionmaker, folders.delete, empty)
    for folder in (full, with_child):
        with pytest.raises(folders.FolderNotEmpty):
            await tx(sessionmaker, folders.delete, folder)
    assert names_under(db_conn, a) == ["Full", "Parent"]
    with pytest.raises(folders.FolderNotFound):
        await tx(sessionmaker, folders.delete, empty)


async def test_delete_moves_contents_to_parent_and_merges_clashes(sessionmaker, db_conn) -> None:
    # A
    # ├── X (1 file)              <- deleted
    # │   ├── Cats (2 files)      <- clashes with A / Cats: merged
    # │   │   └── Kittens (1)     <- clashes with A / Cats / Kittens: merged again
    # │   ├── Birds (1 file)      <- no clash: moved under A
    # │   └── X (1 file)          <- same name as the deleted folder: moved under A
    # ├── Cats (1 file)
    # │   └── Kittens (1 file)
    a = make_folder(db_conn, "A")
    x = make_folder(db_conn, "X", a)
    x_cats = make_folder(db_conn, "cats", x)
    x_kittens = make_folder(db_conn, "Kittens", x_cats)
    birds = make_folder(db_conn, "Birds", x)
    x_x = make_folder(db_conn, "X", x)
    cats = make_folder(db_conn, "Cats", a)
    kittens = make_folder(db_conn, "Kittens", cats)
    in_x = fake_archived(db_conn, x, 1, "x")
    in_x_cats = fake_archived(db_conn, x_cats, 2, "xc")
    in_x_kittens = fake_archived(db_conn, x_kittens, 1, "xk")
    in_birds = fake_archived(db_conn, birds, 1, "b")
    in_x_x = fake_archived(db_conn, x_x, 1, "xx")
    fake_archived(db_conn, cats, 1, "c")
    fake_archived(db_conn, kittens, 1, "k")
    total = db_conn.execute("SELECT count(*) FROM files").fetchone()[0]

    result = await tx(sessionmaker, folders.delete, x, move_contents_to_parent=True)

    assert result.as_json() == {"files_moved": 4, "folders_moved": 2, "folders_merged": 2}
    assert names_under(db_conn, a) == ["Birds", "Cats", "X"]
    assert names_under(db_conn, cats) == ["Kittens"]
    assert all(folder_of(db_conn, f) == a for f in in_x)
    assert all(folder_of(db_conn, f) == cats for f in in_x_cats)
    assert all(folder_of(db_conn, f) == kittens for f in in_x_kittens)
    assert all(folder_of(db_conn, f) == birds for f in in_birds)
    assert all(folder_of(db_conn, f) == x_x for f in in_x_x)
    # No file lost its folder, no folder points at a deleted one.
    assert db_conn.execute(
        "SELECT count(*) FROM files WHERE folder_id IS NULL OR folder_id NOT IN"
        " (SELECT id FROM folders)"
    ).fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM files").fetchone()[0] == total
    async with sessionmaker() as session:
        nodes = node_by_path(await folders.get_tree(session))
    assert (nodes["A / Cats"].direct_count, nodes["A / Cats"].subtree_count) == (3, 5)


async def test_delete_top_level_moves_files_to_inbox(sessionmaker, db_conn) -> None:
    box = inbox(db_conn)
    top = make_folder(db_conn, "Top")
    sub = make_folder(db_conn, "Sub", top)
    later = make_folder(db_conn, "inbox", top)  # clashes with the top-level Inbox: merged
    files_top = fake_archived(db_conn, top, 2, "t")
    files_later = fake_archived(db_conn, later, 1, "l")

    await tx(sessionmaker, folders.delete, top, move_contents_to_parent=True)

    assert all(folder_of(db_conn, f) == box for f in files_top + files_later)
    assert names_under(db_conn, None) == ["Inbox", "Sub"]
    assert db_conn.execute("SELECT parent_id FROM folders WHERE id = %s", (sub,)).fetchone() == (
        None,
    )


async def test_recent_folders(sessionmaker, db_conn) -> None:
    a = make_folder(db_conn, "A")
    b = make_folder(db_conn, "B", a)
    old = fake_archived(db_conn, a, 1)
    new = fake_archived(db_conn, b, 1)
    db_conn.execute(
        "UPDATE files SET archived_at = now() - interval '1 day' WHERE id = %s", (old[0],)
    )
    async with sessionmaker() as session:
        recent = await folders.recent_folders(session)
    assert [(f.id, f.path) for f in recent] == [(b, "A / B"), (a, "A")]
    assert new
