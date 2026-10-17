"""Library classification: moving archived files between folders, bulk tags, details."""

import os

import pytest

from app.library import classify
from app.review import service
from tests.library.helpers import fake_archived, folder_of, make_folder, tag_rows, tx
from tests.review.helpers import decide, inbox, pending

pytestmark = pytest.mark.usefixtures("clean_ingest")


def snapshot(root) -> dict[str, tuple[int, int, int]]:
    """Every file under DATA_DIR with (inode, size, mtime_ns)."""
    out = {}
    for dirpath, _, names in os.walk(root):
        for name in names:
            st = os.stat(os.path.join(dirpath, name))
            out[os.path.join(dirpath, name)] = (st.st_ino, st.st_size, st.st_mtime_ns)
    return out


async def test_moving_archived_files_does_not_touch_the_filesystem(
    sessionmaker, paths, db_conn
) -> None:
    ids = await pending(sessionmaker, paths, db_conn, 3)
    await decide(sessionmaker, service.archive, paths, ids)
    target = make_folder(db_conn, "Elsewhere")
    before = snapshot(paths.root)

    result = await tx(sessionmaker, classify.move_files, ids, target)

    assert result.ok_ids == ids and not result.errors
    assert all(folder_of(db_conn, f) == target for f in ids)
    assert snapshot(paths.root) == before


async def test_move_rejects_files_that_are_not_archived(sessionmaker, paths, db_conn) -> None:
    (pending_id,) = await pending(sessionmaker, paths, db_conn, 1)
    (archived_id,) = fake_archived(db_conn, inbox(db_conn), 1)
    target = make_folder(db_conn, "T")

    result = await tx(sessionmaker, classify.move_files, [pending_id, archived_id, 999999], target)

    assert result.ok_ids == [archived_id]
    assert set(result.errors) == {pending_id, 999999}
    assert "pending review, not archived" in result.errors[pending_id]
    assert folder_of(db_conn, pending_id) is None


async def test_bulk_add_and_remove_tags(sessionmaker, db_conn) -> None:
    a, b = fake_archived(db_conn, inbox(db_conn), 2)
    await tx(sessionmaker, classify.add_tags_to_files, [a, b], ["Red", "blue sky"])
    await tx(sessionmaker, classify.remove_tags_from_files, [a], ["RED", "unknown"])
    assert tag_rows(db_conn, a) == {"red": ("user", True), "blue_sky": ("user", False)}
    assert tag_rows(db_conn, b) == {"red": ("user", False), "blue_sky": ("user", False)}
    await tx(sessionmaker, classify.add_tags_to_files, [a], ["red"])
    assert tag_rows(db_conn, a)["red"] == ("user", False)
    with pytest.raises(classify.ClassifyError):
        await tx(sessionmaker, classify.add_tags_to_files, [a], ["  "])


async def test_update_details(sessionmaker, paths, db_conn) -> None:
    (a,) = fake_archived(db_conn, inbox(db_conn), 1)
    (p,) = await pending(sessionmaker, paths, db_conn, 1)
    folder = make_folder(db_conn, "F")

    await tx(sessionmaker, classify.update_details, a, {"title": " Hello ", "folder_id": folder})
    row = db_conn.execute("SELECT title, description, folder_id FROM files WHERE id = %s", (a,))
    assert row.fetchone() == ("Hello", None, folder)
    await tx(sessionmaker, classify.update_details, a, {"title": "", "description": "Text"})
    row = db_conn.execute("SELECT title, description FROM files WHERE id = %s", (a,))
    assert row.fetchone() == (None, "Text")

    with pytest.raises(classify.ClassifyError, match="only archived files"):
        await tx(sessionmaker, classify.update_details, p, {"folder_id": folder})
    with pytest.raises(classify.ClassifyError, match="must be in a folder"):
        await tx(sessionmaker, classify.update_details, a, {"folder_id": None})
    with pytest.raises(LookupError):
        await tx(sessionmaker, classify.update_details, 999999, {"title": "x"})
