"""Review decisions and undo, including where the originals are on disk after commit."""

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from app.db.models_files import IngestOutcome
from app.ingest.origin import IngestOrigin
from app.review import service
from app.review.trash import purge_trash
from tests.ingest.helpers import one, run
from tests.media import structured_png, variant_jpeg, variant_resized
from tests.review.helpers import active_tags, decide, file_row, inbox, locations, pending

pytestmark = pytest.mark.usefixtures("clean_ingest")


def action_rows(conn: psycopg.Connection, file_id: int) -> list[tuple]:
    return conn.execute(
        "SELECT action::text, previous_status::text, previous_folder_id, undone"
        " FROM review_actions WHERE file_id = %s ORDER BY id",
        (file_id,),
    ).fetchall()


def make_folder(conn: psycopg.Connection, name: str, parent: int | None = None) -> int:
    return one(
        conn, "INSERT INTO folders (parent_id, name) VALUES (%s, %s) RETURNING id", (parent, name)
    )[0]


async def test_archive_moves_into_archive_and_inbox(sessionmaker, paths, db_conn) -> None:
    (fid,) = await pending(sessionmaker, paths, db_conn, 1)
    before = file_row(db_conn, fid)
    assert locations(paths, before) == {"staging": True, "archive": False, "trash": False}

    result = await decide(sessionmaker, service.archive, paths, [fid])

    assert result.ok and result.results[0].status == "archived"
    row = file_row(db_conn, fid)
    assert row["status"] == "archived"
    assert row["folder_id"] == inbox(db_conn)
    assert row["staging_path"] is None
    assert row["reviewed_at"] is not None and row["archived_at"] == row["reviewed_at"]
    assert paths.archive_path(row["sha"], row["ext"]).is_file()
    assert not paths.resolve(before["staging_path"]).exists()
    assert paths.archive_path(row["sha"], "png").relative_to(paths.archive).parts[:2] == (
        row["sha"][0:2],
        row["sha"][2:4],
    )
    assert action_rows(db_conn, fid) == [("archive", "pending_review", None, False)]


async def test_archive_with_folder_and_user_tags(sessionmaker, paths, db_conn) -> None:
    origin = IngestOrigin(tags=["Crawled Tag", "gone"])
    await run(sessionmaker, paths, structured_png(5), "t.png", origin)
    fid = one(db_conn, "SELECT id FROM files")[0]
    db_conn.execute(
        "UPDATE file_tags SET removed = true"
        " WHERE tag_id = (SELECT id FROM tags WHERE name = 'gone')"
    )
    folder = make_folder(db_conn, "Pictures")

    result = await decide(
        sessionmaker, service.archive, paths, [fid], folder, ["crawled tag", "New One", "gone"]
    )

    assert result.ok
    assert file_row(db_conn, fid)["folder_id"] == folder
    # An active crawler tag stays crawler; new and re-added tags become user tags.
    assert active_tags(db_conn, fid) == {
        "crawled_tag": "crawler",
        "new_one": "user",
        "gone": "user",
    }

    undone = await decide(sessionmaker, service.undo, paths, result.action_ids)
    assert undone.ok
    assert active_tags(db_conn, fid) == {"crawled_tag": "crawler"}
    removed = one(
        db_conn,
        "SELECT ft.removed, ft.origin::text FROM file_tags ft JOIN tags t ON t.id = ft.tag_id"
        " WHERE ft.file_id = %s AND t.name = 'gone'",
        (fid,),
    )
    assert removed == (True, "crawler")


async def test_archive_into_unknown_folder_is_refused(sessionmaker, paths, db_conn) -> None:
    (fid,) = await pending(sessionmaker, paths, db_conn, 1)
    with pytest.raises(service.ReviewError, match="folder 999999 does not exist"):
        await decide(sessionmaker, service.archive, paths, [fid], 999999)
    assert file_row(db_conn, fid)["status"] == "pending_review"


async def test_discard_moves_to_trash_and_keeps_hashes(sessionmaker, paths, db_conn) -> None:
    (fid,) = await pending(sessionmaker, paths, db_conn, 1)
    before = file_row(db_conn, fid)

    result = await decide(sessionmaker, service.discard, paths, [fid])

    assert result.ok
    row = file_row(db_conn, fid)
    assert row["status"] == "discarded"
    assert row["discarded_at"] is not None and row["staging_path"] is None
    assert locations(paths, {**row, "staging_path": None}) == {
        "staging": False,
        "archive": False,
        "trash": True,
    }
    assert not paths.resolve(before["staging_path"]).exists()
    assert row["has_thumb"] and paths.thumb_path(row["sha"]).is_file()
    assert row["phash"] == before["phash"] is not None


async def test_undo_archive_and_discard_move_files_back(sessionmaker, paths, db_conn) -> None:
    a, d = await pending(sessionmaker, paths, db_conn, 2)
    staged = {f: file_row(db_conn, f)["staging_path"] for f in (a, d)}
    archived = await decide(sessionmaker, service.archive, paths, [a])
    discarded = await decide(sessionmaker, service.discard, paths, [d])

    result = await decide(
        sessionmaker, service.undo, paths, archived.action_ids + discarded.action_ids
    )

    assert result.ok and {r.file_id for r in result.results} == {a, d}
    for fid in (a, d):
        row = file_row(db_conn, fid)
        assert row["status"] == "pending_review"
        assert row["folder_id"] is None
        assert row["staging_path"] == staged[fid]
        assert (row["reviewed_at"], row["archived_at"], row["discarded_at"]) == (None, None, None)
        assert locations(paths, row) == {"staging": True, "archive": False, "trash": False}
    assert [r[3] for r in action_rows(db_conn, a)] == [True]

    again = await decide(sessionmaker, service.undo, paths, archived.action_ids)
    assert not again.ok and "already undone" in (again.results[0].error or "")


async def test_undo_never_overwrites_a_reused_staging_name(sessionmaker, paths, db_conn) -> None:
    (fid,) = await pending(sessionmaker, paths, db_conn, 1)
    staged = file_row(db_conn, fid)["staging_path"]
    result = await decide(sessionmaker, service.discard, paths, [fid])
    paths.resolve(staged).write_bytes(b"another file")

    await decide(sessionmaker, service.undo, paths, result.action_ids)

    row = file_row(db_conn, fid)
    assert row["status"] == "pending_review" and row["staging_path"] != staged
    assert row["staging_path"].startswith(staged.removesuffix(".png") + "-")
    assert paths.resolve(staged).read_bytes() == b"another file"
    assert locations(paths, row)["staging"]


async def test_discard_archived_and_undo(sessionmaker, paths, db_conn) -> None:
    (fid,) = await pending(sessionmaker, paths, db_conn, 1)
    folder = make_folder(db_conn, "Keep")
    await decide(sessionmaker, service.archive, paths, [fid], folder)
    archived_at = file_row(db_conn, fid)["archived_at"]

    wrong = await decide(sessionmaker, service.discard, paths, [fid])
    assert not wrong.ok and "is archived, not pending review" in (wrong.results[0].error or "")
    result = await decide(sessionmaker, service.discard_archived, paths, [fid])

    assert result.ok
    row = file_row(db_conn, fid)
    assert row["status"] == "discarded" and row["folder_id"] is None
    assert locations(paths, row) == {"staging": False, "archive": False, "trash": True}
    assert action_rows(db_conn, fid)[-1] == ("discard", "archived", folder, False)

    await decide(sessionmaker, service.undo, paths, result.action_ids)
    row = file_row(db_conn, fid)
    assert row["status"] == "archived" and row["folder_id"] == folder
    assert row["archived_at"] == archived_at
    assert locations(paths, row) == {"staging": False, "archive": True, "trash": False}


async def test_only_the_latest_action_of_a_file_can_be_undone(sessionmaker, paths, db_conn) -> None:
    (fid,) = await pending(sessionmaker, paths, db_conn, 1)
    first = await decide(sessionmaker, service.archive, paths, [fid])
    await decide(sessionmaker, service.discard_archived, paths, [fid])

    result = await decide(sessionmaker, service.undo, paths, first.action_ids)

    assert not result.ok
    assert "a later decision" in (result.results[0].error or "")
    assert file_row(db_conn, fid)["status"] == "discarded"


async def test_replace_and_undo(sessionmaker, paths, db_conn) -> None:
    original = structured_png(11)
    await run(
        sessionmaker, paths, variant_resized(original), "small.png", IngestOrigin(tags=["c1"])
    )
    old = one(db_conn, "SELECT id FROM files WHERE original_filename = 'small.png'")[0]
    folder = make_folder(db_conn, "Art")
    await decide(sessionmaker, service.archive, paths, [old], folder, ["u1", "shared"])
    await run(
        sessionmaker, paths, original, "big.png", IngestOrigin(tags=["c2", "shared"])
    )  # crawler origin for "shared" on the new file
    new = one(db_conn, "SELECT id FROM files WHERE original_filename = 'big.png'")[0]
    new_staging = file_row(db_conn, new)["staging_path"]

    result = await decide(sessionmaker, service.replace, paths, new, old)

    assert result.ok and [r.status for r in result.results] == ["archived", "discarded"]
    new_row, old_row = file_row(db_conn, new), file_row(db_conn, old)
    assert new_row["status"] == "archived" and new_row["folder_id"] == folder
    assert old_row["status"] == "discarded"
    assert locations(paths, new_row) == {"staging": False, "archive": True, "trash": False}
    assert locations(paths, old_row) == {"staging": False, "archive": False, "trash": True}
    # Union of active tags; the new file keeps its own origin for a tag both have.
    assert active_tags(db_conn, new) == {
        "c1": "crawler",
        "c2": "crawler",
        "u1": "user",
        "shared": "crawler",
    }
    assert active_tags(db_conn, old) == {"c1": "crawler", "u1": "user", "shared": "user"}
    (groups,) = one(
        db_conn, "SELECT count(DISTINCT group_id) FROM review_actions WHERE action = 'replace'"
    )
    assert groups == 1

    # Undoing one row of the replace undoes both.
    undone = await decide(sessionmaker, service.undo, paths, result.action_ids[:1])
    assert undone.ok and {r.file_id for r in undone.results} == {new, old}
    new_row, old_row = file_row(db_conn, new), file_row(db_conn, old)
    assert new_row["status"] == "pending_review" and new_row["staging_path"] == new_staging
    assert old_row["status"] == "archived" and old_row["folder_id"] == folder
    assert locations(paths, new_row) == {"staging": True, "archive": False, "trash": False}
    assert locations(paths, old_row) == {"staging": False, "archive": True, "trash": False}
    assert active_tags(db_conn, new) == {"c2": "crawler", "shared": "crawler"}


async def test_replace_needs_pending_new_and_archived_old(sessionmaker, paths, db_conn) -> None:
    a, b = await pending(sessionmaker, paths, db_conn, 2)
    result = await decide(sessionmaker, service.replace, paths, a, b)
    assert not result.ok
    assert all("is pending review, not archived" in (r.error or "") for r in result.results)
    assert file_row(db_conn, a)["status"] == file_row(db_conn, b)["status"] == "pending_review"


async def test_undo_of_purged_discard_is_refused(sessionmaker, paths, db_conn) -> None:
    (fid,) = await pending(sessionmaker, paths, db_conn, 1)
    result = await decide(sessionmaker, service.discard, paths, [fid])
    db_conn.execute(
        "UPDATE files SET discarded_at = now() - interval '30 days' WHERE id = %s", (fid,)
    )
    async with sessionmaker() as session:
        stats = await purge_trash(session, paths, retention_days=7)
    assert stats.purged == 1

    undone = await decide(sessionmaker, service.undo, paths, result.action_ids)

    assert not undone.ok
    assert undone.results[0].error == (
        f"the original of file {fid} (p0.png) is no longer in the trash (purged after the "
        "retention period); the discard cannot be undone"
    )
    row = file_row(db_conn, fid)
    assert row["status"] == "discarded"
    assert action_rows(db_conn, fid)[-1][3] is False


async def test_batch_with_invalid_files(sessionmaker, paths, db_conn) -> None:
    a, b, c = await pending(sessionmaker, paths, db_conn, 3)
    await decide(sessionmaker, service.archive, paths, [b])

    result = await decide(sessionmaker, service.archive, paths, [a, b, 999999, c, a])

    assert [(r.file_id, r.ok) for r in result.results] == [
        (a, True),
        (b, False),
        (999999, False),
        (c, True),
    ]
    assert "is archived, not pending review" in (result.results[1].error or "")
    assert result.results[2].error == "file 999999 does not exist"
    assert len(result.action_ids) == 2
    for fid in (a, b, c):
        assert file_row(db_conn, fid)["status"] == "archived"

    discard = await decide(sessionmaker, service.discard, paths, [a, 424242])
    assert [(r.file_id, r.ok) for r in discard.results] == [(a, False), (424242, False)]

    undo = await decide(sessionmaker, service.undo, paths, [*result.action_ids, 777777])
    assert sum(r.ok for r in undo.results) == 2
    assert any(r.error == "action 777777 does not exist" for r in undo.results)


async def test_missing_staging_original_is_reported(sessionmaker, paths, db_conn) -> None:
    a, b = await pending(sessionmaker, paths, db_conn, 2)
    paths.resolve(file_row(db_conn, a)["staging_path"]).unlink()

    result = await decide(sessionmaker, service.archive, paths, [a, b])

    assert [r.ok for r in result.results] == [False, True]
    assert "is missing" in (result.results[0].error or "")
    assert file_row(db_conn, a)["status"] == "pending_review"


async def test_rolled_back_decision_moves_nothing(sessionmaker, paths, db_conn) -> None:
    (fid,) = await pending(sessionmaker, paths, db_conn, 1)
    async with sessionmaker() as session:
        result = await service.archive(session, paths, [fid])
        assert result.ok
        await session.rollback()
    row = file_row(db_conn, fid)
    assert row["status"] == "pending_review"
    assert locations(paths, row) == {"staging": True, "archive": False, "trash": False}


async def test_compare_keep_left_discards_right(sessionmaker, paths, db_conn) -> None:
    original = structured_png(21)
    await run(sessionmaker, paths, original, "orig.png")
    await run(sessionmaker, paths, variant_resized(original), "half.png")
    left = one(db_conn, "SELECT id FROM files WHERE original_filename = 'orig.png'")[0]
    right = one(db_conn, "SELECT id FROM files WHERE original_filename = 'half.png'")[0]

    result = await decide(sessionmaker, service.compare_decision, paths, left, right, "keep_left")

    assert [(r.file_id, r.status) for r in result.results] == [
        (left, "archived"),
        (right, "discarded"),
    ]
    assert locations(paths, file_row(db_conn, left))["archive"]
    assert locations(paths, {**file_row(db_conn, right), "staging_path": None})["trash"]


async def test_compare_keep_both_dismisses_the_pair(sessionmaker, paths, db_conn) -> None:
    original = structured_png(22)
    await run(sessionmaker, paths, original, "orig.png")
    await run(sessionmaker, paths, variant_jpeg(original), "q60.jpg")
    left, right = (r[0] for r in db_conn.execute("SELECT id FROM files ORDER BY id"))

    result = await decide(sessionmaker, service.compare_decision, paths, right, left, "keep_both")

    assert result.ok and len(result.action_ids) == 2
    assert file_row(db_conn, left)["status"] == file_row(db_conn, right)["status"] == "archived"
    assert one(db_conn, "SELECT bool_and(dismissed) FROM similar_pairs") == (True,)


async def test_compare_refusals_change_nothing(sessionmaker, paths, db_conn) -> None:
    a, b = await pending(sessionmaker, paths, db_conn, 2)
    await decide(sessionmaker, service.discard, paths, [b])
    with pytest.raises(service.ReviewError, match="is discarded; it cannot be kept"):
        await decide(sessionmaker, service.compare_decision, paths, a, b, "keep_right")
    with pytest.raises(service.ReviewError, match="is discarded, not archived"):
        await decide(sessionmaker, service.compare_decision, paths, a, b, "replace")
    assert file_row(db_conn, a)["status"] == "pending_review"
    assert locations(paths, file_row(db_conn, a))["staging"]

    # A discarded right file stays discarded when the left one is kept.
    result = await decide(sessionmaker, service.compare_decision, paths, a, b, "keep_left")
    assert [(r.file_id, r.status) for r in result.results] == [(a, "archived")]


async def test_forced_reingest_blocks_undo_of_the_discard(sessionmaker, paths, db_conn) -> None:
    data = structured_png(31)
    await run(sessionmaker, paths, data, "x.png")
    fid = one(db_conn, "SELECT id FROM files")[0]
    result = await decide(sessionmaker, service.discard, paths, [fid])
    outcome, _ = await run(sessionmaker, paths, data, "x.png", IngestOrigin(force=True))
    assert outcome is IngestOutcome.forced_reingest

    undone = await decide(sessionmaker, service.undo, paths, result.action_ids)

    assert not undone.ok and "is pending review now" in (undone.results[0].error or "")


async def test_discarded_files_still_count_as_duplicates(sessionmaker, paths, db_conn) -> None:
    original = structured_png(41)
    await run(sessionmaker, paths, original, "first.png")
    first = one(db_conn, "SELECT id FROM files")[0]
    await decide(sessionmaker, service.discard, paths, [first])

    outcome, _ = await run(sessionmaker, paths, original, "again.png")
    assert outcome is IngestOutcome.duplicate_exact
    outcome, _ = await run(sessionmaker, paths, variant_jpeg(original), "variant.jpg")
    assert outcome is IngestOutcome.new
    variant = one(db_conn, "SELECT id FROM files WHERE original_filename = 'variant.jpg'")[0]
    pair = one(db_conn, "SELECT file_a_id, file_b_id, dismissed FROM similar_pairs")
    assert pair == (first, variant, False)


async def test_purge_trash_respects_retention(sessionmaker, paths, db_conn) -> None:
    old, recent, kept = await pending(sessionmaker, paths, db_conn, 3)
    await decide(sessionmaker, service.discard, paths, [old, recent])
    db_conn.execute(
        "UPDATE files SET discarded_at = now() - interval '8 days' WHERE id = %s", (old,)
    )
    db_conn.execute(
        "UPDATE files SET discarded_at = now() - interval '6 days' WHERE id = %s", (recent,)
    )

    async with sessionmaker() as session:
        stats = await purge_trash(session, paths, retention_days=7)
        again = await purge_trash(session, paths, retention_days=7)

    assert stats.as_dict() == {"expired": 1, "purged": 1, "errors": 0}
    assert again.as_dict() == {"expired": 1, "purged": 0, "errors": 0}
    old_row, recent_row = file_row(db_conn, old), file_row(db_conn, recent)
    assert not paths.trash_path(old_row["sha"], old_row["ext"]).exists()
    assert paths.trash_path(recent_row["sha"], recent_row["ext"]).is_file()
    # The row, hashes and thumbnail of a purged file stay.
    assert old_row["status"] == "discarded" and old_row["phash"] is not None
    assert paths.thumb_path(old_row["sha"]).is_file()
    assert locations(paths, file_row(db_conn, kept))["staging"]

    future = datetime.now(UTC) + timedelta(days=30)
    async with sessionmaker() as session:
        later = await purge_trash(session, paths, retention_days=7, now=future)
    assert later.purged == 1


@pytest.mark.usefixtures("settings")
async def test_purge_trash_task_is_daily_and_uses_settings(sessionmaker, paths, db_conn) -> None:
    from app.db.models_crawl import JobKind
    from app.jobs.tasks import purge_trash as purge_task
    from app.ops.schedule import FIXED_SCHEDULES

    assert FIXED_SCHEDULES[JobKind.purge_trash] == "30 3 * * *"  # daily, APP_TIMEZONE

    a, b = await pending(sessionmaker, paths, db_conn, 2)
    await decide(sessionmaker, service.discard, paths, [a, b])
    db_conn.execute(
        "UPDATE files SET discarded_at = now() - interval '7 days 1 minute' WHERE id = %s", (a,)
    )

    assert await purge_task() == "succeeded"

    stats = db_conn.execute("SELECT stats FROM job_runs WHERE kind = 'purge_trash'").fetchone()[0]
    assert stats == {"expired": 1, "purged": 1, "errors": 0, "retention_days": 7}
    assert not paths.trash_path(file_row(db_conn, a)["sha"], "png").exists()
    assert paths.trash_path(file_row(db_conn, b)["sha"], "png").exists()


def test_purge_trash_cli(capsys, sessionmaker) -> None:
    from app.cli import main

    assert main(["purge-trash"]) == 0
    assert capsys.readouterr().out.strip() == "purge-trash: expired 0, purged 0, errors 0"


async def test_archive_with_folder_and_tag_changes_in_one_call(
    sessionmaker, paths, db_conn
) -> None:
    origin = IngestOrigin(tags=["keep me", "drop me", "drop too"])
    await run(sessionmaker, paths, structured_png(40), "panel.png", origin)
    fid = one(db_conn, "SELECT id FROM files")[0]
    parent = make_folder(db_conn, "Animals")
    nested = make_folder(db_conn, "Cats", parent)

    result = await decide(
        sessionmaker,
        service.archive,
        paths,
        [fid],
        nested,
        ["Mine", "drop too"],
        {fid: ["Drop Me", "drop too", "never existed"]},
    )

    assert result.ok
    assert file_row(db_conn, fid)["folder_id"] == nested
    # "drop too" is removed and then added back by the user in the same call.
    assert active_tags(db_conn, fid) == {"keep_me": "crawler", "mine": "user", "drop_too": "user"}
    removed = db_conn.execute(
        "SELECT t.name, ft.origin::text FROM file_tags ft JOIN tags t ON t.id = ft.tag_id"
        " WHERE ft.file_id = %s AND ft.removed",
        (fid,),
    ).fetchall()
    assert removed == [("drop_me", "crawler")]

    undone = await decide(sessionmaker, service.undo, paths, result.action_ids)
    assert undone.ok
    assert active_tags(db_conn, fid) == {
        "keep_me": "crawler",
        "drop_me": "crawler",
        "drop_too": "crawler",
    }


async def test_bulk_archive_with_folder_and_added_tags(sessionmaker, paths, db_conn) -> None:
    ids = await pending(sessionmaker, paths, db_conn, 3, seed=50)
    folder = make_folder(db_conn, "Bulk")

    result = await decide(sessionmaker, service.archive, paths, ids, folder, ["batch", "Two Words"])

    assert result.ok and len(result.action_ids) == 3
    for fid in ids:
        row = file_row(db_conn, fid)
        assert (row["status"], row["folder_id"]) == ("archived", folder)
        assert active_tags(db_conn, fid) == {"batch": "user", "two_words": "user"}
        assert locations(paths, row)["archive"]
