"""Review API: queue filters and pagination, decisions with per-file results, undo."""

import httpx
import psycopg
import pytest

from app.ingest.origin import IngestOrigin
from tests.ingest.helpers import one, run
from tests.media import structured_png, variant_jpeg, variant_resized
from tests.review.helpers import file_row, inbox, locations, pending

pytestmark = pytest.mark.usefixtures("clean_ingest")


def spread_collected(conn: psycopg.Connection, ids: list[int]) -> None:
    """Give files distinct collected_at values in id order (oldest first)."""
    for i, file_id in enumerate(ids):
        conn.execute(
            "UPDATE files SET collected_at ="
            " timestamptz '2026-01-01 00:00Z' + %s * interval '1 hour' WHERE id = %s",
            (i, file_id),
        )


async def queue_ids(client: httpx.AsyncClient, query: str = "") -> list[int]:
    response = await client.get(f"/api/review/queue?{query}")
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


async def test_queue_order_and_cursor_pagination(
    client: httpx.AsyncClient, sessionmaker, paths, db_conn
) -> None:
    ids = await pending(sessionmaker, paths, db_conn, 5)
    spread_collected(db_conn, ids)
    # Two files collected at the same time: the id breaks the tie.
    db_conn.execute(
        "UPDATE files SET collected_at = (SELECT collected_at FROM files WHERE id = %s)"
        " WHERE id = %s",
        (ids[1], ids[2]),
    )

    assert await queue_ids(client) == ids
    assert await queue_ids(client, "order=newest") == ids[::-1]

    for order, expected in (("oldest", ids), ("newest", ids[::-1])):
        seen, cursor, pages = [], None, 0
        while True:
            query = f"order={order}&limit=2" + (f"&cursor={cursor}" if cursor else "")
            body = (await client.get(f"/api/review/queue?{query}")).json()
            assert body["total"] == 5
            seen += [item["id"] for item in body["items"]]
            pages += 1
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert seen == expected and pages == 3

    item = (await client.get("/api/review/queue?limit=1")).json()["items"][0]
    assert item["id"] == ids[0]
    assert item["status"] == "pending_review"
    assert item["thumb_url"] == f"/media/{ids[0]}/thumb"
    assert item["original_url"] == f"/media/{ids[0]}/original"
    assert item["media_kind"] == "image" and item["width"] == 640
    assert item["similar"] == {"kept": [], "discarded": []}


async def test_queue_filters(client: httpx.AsyncClient, sessionmaker, paths, db_conn) -> None:
    original = structured_png(50)
    await run(sessionmaker, paths, original, "orig.png")
    await run(sessionmaker, paths, variant_resized(original), "half.png", batch="a" * 32)
    await run(
        sessionmaker, paths, structured_png(51), "crawled.png", IngestOrigin(source_key="site")
    )
    await run(sessionmaker, paths, structured_png(52), "clip.png")
    orig, half, crawled, clip = (r[0] for r in db_conn.execute("SELECT id FROM files ORDER BY id"))
    db_conn.execute("UPDATE files SET media_kind = 'video' WHERE id = %s", (clip,))

    assert await queue_ids(client, "media_kind=video") == [clip]
    assert await queue_ids(client, "media_kind=video&media_kind=image") == [
        orig,
        half,
        crawled,
        clip,
    ]
    assert await queue_ids(client, "source_key=site") == [crawled]
    assert await queue_ids(client, "source_key=upload") == [orig, half, clip]
    assert await queue_ids(client, f"batch={'a' * 32}") == [half]
    assert await queue_ids(client, "has_similar=true") == [orig, half]
    assert await queue_ids(client, "has_similar=false") == [crawled, clip]
    assert await queue_ids(client, "similar_to_discarded=true") == []

    response = await client.post("/api/review/discard", json={"file_ids": [orig]})
    assert response.json()["ok"]
    assert await queue_ids(client, "similar_to_discarded=true") == [half]
    assert await queue_ids(client, "similar_to_discarded=false") == [crawled, clip]
    items = (await client.get("/api/review/queue?has_similar=true")).json()["items"]
    assert [(i["id"], i["similar"]) for i in items] == [(half, {"kept": [], "discarded": [orig]})]

    # Empty form values mean "any".
    assert (
        len(await queue_ids(client, "has_similar=&similar_to_discarded=&source_key=&batch=")) == 3
    )

    db_conn.execute(
        "UPDATE files SET status = 'error', error_message = 'boom' WHERE id = %s", (clip,)
    )
    errors = (await client.get("/api/review/queue?status=error")).json()
    assert [(i["id"], i["error_message"]) for i in errors["items"]] == [(clip, "boom")]


@pytest.mark.parametrize(
    "query",
    ["cursor=not-a-cursor", "batch=../etc", "has_similar=maybe", "status=archived", "order=random"],
)
async def test_queue_rejects_bad_parameters(client: httpx.AsyncClient, query: str) -> None:
    response = await client.get(f"/api/review/queue?{query}")
    assert response.status_code in (400, 422)


async def test_archive_discard_and_undo_api(
    client: httpx.AsyncClient, sessionmaker, paths, db_conn
) -> None:
    a, b, c = await pending(sessionmaker, paths, db_conn, 3)

    response = await client.post(
        "/api/review/archive", json={"file_ids": [a, 999999, b], "tags": ["From Review"]}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert [(r["file_id"], r["ok"], r["status"]) for r in body["results"]] == [
        (a, True, "archived"),
        (999999, False, None),
        (b, True, "archived"),
    ]
    assert body["results"][1]["error"] == "file 999999 does not exist"
    assert len(body["action_ids"]) == 2
    assert file_row(db_conn, a)["folder_id"] == inbox(db_conn)
    assert locations(paths, file_row(db_conn, a))["archive"]

    discard = (await client.post("/api/review/discard", json={"file_ids": [c]})).json()
    assert (
        discard["ok"] and locations(paths, {**file_row(db_conn, c), "staging_path": None})["trash"]
    )

    undo = (
        await client.post("/api/review/undo", json={"action_ids": discard["action_ids"]})
    ).json()
    assert undo["ok"] and undo["results"][0]["status"] == "pending_review"
    assert locations(paths, file_row(db_conn, c))["staging"]

    later = (await client.post("/api/review/discard-archived", json={"file_ids": [a, c]})).json()
    assert [r["ok"] for r in later["results"]] == [True, False]

    bad_folder = await client.post(
        "/api/review/archive", json={"file_ids": [c], "folder_id": 424242}
    )
    assert bad_folder.status_code == 400
    assert bad_folder.json()["detail"] == "folder 424242 does not exist"
    assert (await client.post("/api/review/archive", json={"file_ids": []})).status_code == 422


async def test_replace_and_compare_api(
    client: httpx.AsyncClient, sessionmaker, paths, db_conn
) -> None:
    original = structured_png(60)
    await run(sessionmaker, paths, variant_resized(original), "small.png")
    await run(sessionmaker, paths, original, "big.png")
    await run(sessionmaker, paths, variant_jpeg(original), "q60.jpg")
    small, big, q60 = (r[0] for r in db_conn.execute("SELECT id FROM files ORDER BY id"))
    await client.post("/api/review/archive", json={"file_ids": [small]})

    refused = await client.post(
        "/api/review/compare", json={"file_id": q60, "other_id": big, "choice": "replace"}
    )
    assert refused.status_code == 409
    assert "is pending review, not archived" in refused.json()["detail"]
    assert file_row(db_conn, q60)["status"] == "pending_review"

    replaced = (
        await client.post(
            "/api/review/compare", json={"file_id": big, "other_id": small, "choice": "replace"}
        )
    ).json()
    assert [(r["file_id"], r["status"]) for r in replaced["results"]] == [
        (big, "archived"),
        (small, "discarded"),
    ]

    kept = (
        await client.post(
            "/api/review/compare", json={"file_id": q60, "other_id": big, "choice": "keep_right"}
        )
    ).json()
    assert [(r["file_id"], r["status"]) for r in kept["results"]] == [(q60, "discarded")]

    # keep_right left the archived right file alone, so the replace can still be undone.
    undo = (
        await client.post("/api/review/undo", json={"action_ids": replaced["action_ids"]})
    ).json()
    assert undo["ok"]
    assert file_row(db_conn, big)["status"] == "pending_review"
    assert file_row(db_conn, small)["status"] == "archived"
    assert locations(paths, file_row(db_conn, small))["archive"]


async def test_replace_endpoint(client: httpx.AsyncClient, sessionmaker, paths, db_conn) -> None:
    original = structured_png(61)
    await run(sessionmaker, paths, variant_resized(original), "small.png")
    await run(sessionmaker, paths, original, "big.png")
    small, big = (r[0] for r in db_conn.execute("SELECT id FROM files ORDER BY id"))
    await client.post("/api/review/archive", json={"file_ids": [small]})

    body = (await client.post("/api/review/replace", json={"new_id": big, "old_id": small})).json()

    assert body["ok"] and len(body["action_ids"]) == 2
    assert one(db_conn, "SELECT status::text FROM files WHERE id = %s", (small,)) == ("discarded",)
    undo = (await client.post("/api/review/undo", json={"action_ids": body["action_ids"]})).json()
    assert undo["ok"]
    assert one(db_conn, "SELECT status::text FROM files WHERE id = %s", (small,)) == ("archived",)
    assert one(db_conn, "SELECT status::text FROM files WHERE id = %s", (big,)) == (
        "pending_review",
    )
