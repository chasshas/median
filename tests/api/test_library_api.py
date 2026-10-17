"""JSON API of phase 4: folders, tags, per-file tags and details, the library, and archive
with folder and tag changes (the call behind the review page's archive panel)."""

import httpx
import pytest

from app.db.models_files import IngestOutcome
from app.ingest.origin import IngestOrigin
from tests.ingest.helpers import one, run
from tests.library.helpers import fake_archived, folder_of, make_folder, set_tag, tag_id, tag_rows
from tests.media import structured_png
from tests.review.helpers import active_tags, file_row, inbox, locations, pending

pytestmark = pytest.mark.usefixtures("clean_ingest")


def counts(tree: list[dict]) -> dict[str, tuple[int, int]]:
    """path -> (direct, subtree) over the nested JSON tree."""
    out: dict[str, tuple[int, int]] = {}
    for node in tree:
        out[node["path"]] = (node["direct_count"], node["subtree_count"])
        out.update(counts(node["children"]))
    return out


async def tree_counts(client: httpx.AsyncClient) -> dict[str, tuple[int, int]]:
    response = await client.get("/api/folders")
    assert response.status_code == 200
    return counts(response.json()["folders"])


async def test_folder_api(client: httpx.AsyncClient, db_conn) -> None:
    box = inbox(db_conn)
    created = await client.post("/api/folders", json={"name": "Animals"})
    assert created.status_code == 201
    animals = created.json()
    assert animals["path"] == "Animals" and animals["parent_id"] is None
    cats = await client.post("/api/folders", json={"name": "Cats", "parent_id": animals["id"]})
    cats = cats.json()
    assert cats["path"] == "Animals / Cats"

    clash = await client.post("/api/folders", json={"name": "cats", "parent_id": animals["id"]})
    assert clash.status_code == 409 and "already exists" in clash.json()["detail"]
    assert (
        await client.post("/api/folders", json={"name": "x", "parent_id": 999999})
    ).status_code == 404
    assert (await client.post("/api/folders", json={"name": "a/b"})).status_code == 409

    cycle = await client.patch(f"/api/folders/{animals['id']}", json={"parent_id": cats["id"]})
    assert cycle.status_code == 409 and "subfolders" in cycle.json()["detail"]
    for body in ({"name": "Mail"}, {"parent_id": animals["id"]}):
        refused = await client.patch(f"/api/folders/{box}", json=body)
        assert refused.status_code == 409 and "Inbox" in refused.json()["detail"]
    assert (
        await client.delete(f"/api/folders/{box}?move_contents_to_parent=true")
    ).status_code == 409

    renamed = await client.patch(f"/api/folders/{cats['id']}", json={"name": "Felines"})
    assert renamed.json()["path"] == "Animals / Felines"
    moved = await client.patch(f"/api/folders/{cats['id']}", json={"parent_id": None})
    assert moved.json()["path"] == "Felines"
    back = await client.patch(
        f"/api/folders/{cats['id']}", json={"parent_id": animals["id"], "name": "Cats"}
    )
    assert back.json()["path"] == "Animals / Cats"

    resolved = await client.get("/api/folders/resolve", params={"path": "animals/CATS"})
    assert resolved.json()["id"] == cats["id"]
    assert (await client.get("/api/folders/resolve", params={"path": "Nope"})).status_code == 404
    detail = (await client.get(f"/api/folders/{animals['id']}")).json()
    assert detail["subtree_ids"] == [animals["id"], cats["id"]]

    fake_archived(db_conn, cats["id"], 2)
    assert (await tree_counts(client))["Animals"] == (0, 2)
    recent = (await client.get("/api/folders/recent")).json()["folders"]
    assert recent == [{"id": cats["id"], "path": "Animals / Cats"}]

    not_empty = await client.delete(f"/api/folders/{animals['id']}")
    assert not_empty.status_code == 409 and "not empty" in not_empty.json()["detail"]
    deleted = await client.delete(f"/api/folders/{cats['id']}?move_contents_to_parent=true")
    assert deleted.json() == {
        "deleted": cats["id"],
        "files_moved": 2,
        "folders_moved": 0,
        "folders_merged": 0,
    }
    assert (await tree_counts(client))["Animals"] == (2, 2)
    assert db_conn.execute("SELECT count(*) FROM files WHERE folder_id IS NULL").fetchone() == (0,)


async def test_moving_ten_files_updates_counts_immediately(
    client: httpx.AsyncClient, db_conn
) -> None:
    source = make_folder(db_conn, "Source")
    target = make_folder(db_conn, "Target")
    nested = make_folder(db_conn, "Nested", target)
    ids = fake_archived(db_conn, source, 12)
    assert (await tree_counts(client))["Source"] == (12, 12)

    moved = await client.post("/api/library/move", json={"file_ids": ids[:10], "folder_id": nested})

    assert moved.status_code == 200 and moved.json()["ok"]
    after = await tree_counts(client)
    assert after["Source"] == (2, 2)
    assert after["Target"] == (0, 10) and after["Target / Nested"] == (10, 10)
    listed = (await client.get(f"/api/library?folder={target}&sub=1&per_page=100")).json()
    assert listed["total"] == 10 and {i["id"] for i in listed["items"]} == set(ids[:10])
    assert (await client.get(f"/api/library?folder={target}")).json()["total"] == 0

    bad = await client.post("/api/library/move", json={"file_ids": ids, "folder_id": 999999})
    assert bad.status_code == 400


async def test_library_listing_sort_and_pages(client: httpx.AsyncClient, db_conn) -> None:
    folder = make_folder(db_conn, "Sorted")
    ids = fake_archived(db_conn, folder, 5)  # sizes 100..104, names f0..f4
    db_conn.execute("UPDATE files SET title = 'zzz' WHERE id = %s", (ids[0],))
    for i, file_id in enumerate(ids):
        db_conn.execute(
            "UPDATE files SET archived_at = timestamptz '2026-05-01Z' + %s * interval '1 day',"
            " collected_at = timestamptz '2026-01-01Z' - %s * interval '1 day' WHERE id = %s",
            (i, i, file_id),
        )

    async def order(query: str) -> list[int]:
        body = (await client.get(f"/api/library?folder={folder}&{query}")).json()
        return [item["id"] for item in body["items"]]

    assert await order("") == ids[::-1]  # archived, newest first
    assert await order("sort=archived&dir=asc") == ids
    assert await order("sort=collected") == ids
    assert await order("sort=size") == ids[::-1]
    assert await order("sort=name") == [*ids[1:], ids[0]]  # the title counts as the name
    page2 = (await client.get(f"/api/library?folder={folder}&per_page=2&page=2")).json()
    assert [i["id"] for i in page2["items"]] == [ids[2], ids[1]]
    assert (page2["total"], page2["pages"]) == (5, 3)
    everything = (await client.get("/api/library?per_page=100")).json()
    assert everything["total"] == 5


async def test_tags_api_rename_merge_and_delete(client: httpx.AsyncClient, db_conn) -> None:
    a, b, c = fake_archived(db_conn, inbox(db_conn), 3)
    set_tag(db_conn, a, "cats", "crawler")
    set_tag(db_conn, b, "cats", "user")
    set_tag(db_conn, b, "cat", "crawler")
    set_tag(db_conn, c, "cat", "user")
    cats, cat = tag_id(db_conn, "cats"), tag_id(db_conn, "cat")

    listed = (await client.get("/api/tags")).json()["tags"]
    assert [(t["name"], t["count"]) for t in listed] == [("cat", 2), ("cats", 2)]
    found = (await client.get("/api/tags/autocomplete?prefix=CA")).json()["tags"]
    assert [t["name"] for t in found] == ["cat", "cats"]

    warn = await client.post(f"/api/tags/{cats}/rename", json={"name": "Cat"})
    assert warn.status_code == 409
    assert warn.json()["merge_into"] == {"id": cat, "name": "cat", "count": 2}
    assert tag_rows(db_conn, a) == {"cats": ("crawler", False)}  # nothing changed yet

    merged = await client.post(f"/api/tags/{cats}/rename", json={"name": "Cat", "merge": True})
    assert merged.status_code == 200
    assert merged.json() == {"tag": {"id": cat, "name": "cat", "count": 3}, "merged_from": "cats"}
    assert tag_rows(db_conn, a) == {"cat": ("crawler", False)}
    assert tag_rows(db_conn, b) == {"cat": ("user", False)}
    assert tag_rows(db_conn, c) == {"cat": ("user", False)}
    assert [t["name"] for t in (await client.get("/api/tags")).json()["tags"]] == ["cat"]

    plain = await client.post(f"/api/tags/{cat}/rename", json={"name": "Feline"})
    assert plain.json()["merged_from"] is None and plain.json()["tag"]["name"] == "feline"
    assert (await client.post(f"/api/tags/{cat}/rename", json={"name": " "})).status_code == 400
    assert (await client.post("/api/tags/999999/rename", json={"name": "x"})).status_code == 404

    in_use = await client.delete(f"/api/tags/{cat}")
    assert in_use.status_code == 409 and "used by 3 file" in in_use.json()["detail"]
    gone = await client.delete(f"/api/tags/{cat}?force=true")
    assert gone.json()["deleted"]["name"] == "feline"
    assert (await client.get("/api/tags")).json()["tags"] == []


async def test_removed_crawler_tag_stays_removed_after_duplicate_ingest(
    client: httpx.AsyncClient, sessionmaker, paths, db_conn
) -> None:
    data = structured_png(77)
    first = IngestOrigin(source_key="example_addon", remote_id="post-1", tags=["Cat", "Outdoor"])
    outcome, _ = await run(sessionmaker, paths, data, "post1.png", first)
    assert outcome is IngestOutcome.new
    fid = one(db_conn, "SELECT id FROM files")[0]
    tags = (await client.get(f"/api/files/{fid}/tags")).json()
    cat = next(t for t in tags["active"] if t["name"] == "cat")
    assert cat["origin"] == "crawler"

    removed = await client.delete(f"/api/files/{fid}/tags/{cat['id']}")
    assert removed.status_code == 200
    assert [t["name"] for t in removed.json()["removed"]] == ["cat"]

    # The same add-on finds the same content again under another post.
    again = IngestOrigin(
        source_key="example_addon", remote_id="post-2", tags=["cat", "outdoor", "Night"]
    )
    outcome, _ = await run(sessionmaker, paths, data, "post2.png", again)
    assert outcome is IngestOutcome.duplicate_exact

    after = (await client.get(f"/api/files/{fid}/tags")).json()
    assert [t["name"] for t in after["active"]] == ["night", "outdoor"]
    assert after["removed"] == [{"id": cat["id"], "name": "cat", "origin": "crawler"}]
    assert db_conn.execute(
        "SELECT count(*) FROM file_sources WHERE file_id = %s", (fid,)
    ).fetchone() == (2,)

    restored = await client.post(f"/api/files/{fid}/tags/{cat['id']}/restore")
    assert [t["name"] for t in restored.json()["active"]] == ["cat", "night", "outdoor"]
    added = await client.post(f"/api/files/{fid}/tags", json={"name": "My Pick"})
    assert {"id": tag_id(db_conn, "my_pick"), "name": "my_pick", "origin": "user"} in added.json()[
        "active"
    ]
    assert (await client.post(f"/api/files/{fid}/tags", json={"name": "  "})).status_code == 400
    assert (await client.delete(f"/api/files/{fid}/tags/999999")).status_code == 404
    assert (await client.get("/api/files/999999/tags")).status_code == 404


async def test_archive_api_with_nested_folder_and_tag_edits(
    client: httpx.AsyncClient, sessionmaker, paths, db_conn
) -> None:
    """What the archive panel sends: a nested folder, one crawler tag removed, one user tag."""
    origin = IngestOrigin(source_key="example_addon", tags=["sunset", "beach"])
    await run(sessionmaker, paths, structured_png(78), "panel.png", origin)
    fid = one(db_conn, "SELECT id FROM files")[0]
    parent = make_folder(db_conn, "Travel")
    nested = make_folder(db_conn, "Coast", parent)

    response = await client.post(
        "/api/review/archive",
        json={
            "file_ids": [fid],
            "folder_id": nested,
            "add_tags": ["Favourite"],
            "remove_tags_per_file": {str(fid): ["beach"]},
        },
    )

    assert response.status_code == 200 and response.json()["ok"], response.text
    row = file_row(db_conn, fid)
    assert (row["status"], row["folder_id"]) == ("archived", nested)
    assert locations(paths, row)["archive"]
    assert active_tags(db_conn, fid) == {"sunset": "crawler", "favourite": "user"}
    assert tag_rows(db_conn, fid)["beach"] == ("crawler", True)


async def test_bulk_archive_api_and_phase3_tags_field(
    client: httpx.AsyncClient, sessionmaker, paths, db_conn
) -> None:
    ids = await pending(sessionmaker, paths, db_conn, 3, seed=90)
    folder = make_folder(db_conn, "Bulk")
    response = await client.post(
        "/api/review/archive",
        json={"file_ids": ids[:2], "folder_id": folder, "add_tags": ["one", "two"]},
    )
    assert response.json()["ok"]
    for fid in ids[:2]:
        assert folder_of(db_conn, fid) == folder
        assert active_tags(db_conn, fid) == {"one": "user", "two": "user"}
    legacy = await client.post("/api/review/archive", json={"file_ids": [ids[2]], "tags": ["old"]})
    assert legacy.json()["ok"] and active_tags(db_conn, ids[2]) == {"old": "user"}
    assert folder_of(db_conn, ids[2]) == inbox(db_conn)


async def test_library_bulk_tags_and_file_details(client: httpx.AsyncClient, db_conn) -> None:
    box = inbox(db_conn)
    a, b = fake_archived(db_conn, box, 2)
    other = make_folder(db_conn, "Other")

    added = await client.post("/api/library/tags/add", json={"file_ids": [a, b], "tags": ["Red"]})
    assert added.json()["ok"]
    removed = await client.post("/api/library/tags/remove", json={"file_ids": [a], "tags": ["red"]})
    assert removed.json()["ok"]
    assert tag_rows(db_conn, a) == {"red": ("user", True)}
    assert tag_rows(db_conn, b) == {"red": ("user", False)}
    empty = await client.post("/api/library/tags/add", json={"file_ids": [a], "tags": ["_"]})
    assert empty.status_code == 400

    patched = await client.patch(
        f"/api/files/{a}", json={"title": "  Sunset  ", "description": "", "folder_id": other}
    )
    assert patched.json() == {"id": a, "title": "Sunset", "description": None, "folder_id": other}
    only_title = await client.patch(f"/api/files/{a}", json={"title": "Dawn"})
    assert only_title.json()["folder_id"] == other
    assert (await client.patch(f"/api/files/{a}", json={"folder_id": 999999})).status_code == 400
    assert (await client.patch(f"/api/files/{a}", json={"folder_id": None})).status_code == 400
    assert (await client.patch("/api/files/999999", json={"title": "x"})).status_code == 404

    discarded = await client.post("/api/review/discard-archived", json={"file_ids": [b]})
    # The fake row has no original on disk, so the discard is refused per file.
    assert discarded.json()["results"][0]["ok"] is False
