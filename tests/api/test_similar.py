import httpx
import psycopg
import pytest

from app.jobs import run_queued_jobs
from tests.media import structured_png, variant_jpeg, variant_resized

pytestmark = pytest.mark.usefixtures("clean_ingest")


async def upload(client: httpx.AsyncClient, name: str, data: bytes) -> None:
    response = await client.post("/api/uploads", files=[("files", (name, data, "image/png"))])
    assert response.status_code == 202
    await run_queued_jobs()


def ids(conn: psycopg.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT id FROM files ORDER BY id").fetchall()]


async def test_similar_api_and_dismiss(client: httpx.AsyncClient, db_conn) -> None:
    original = structured_png(21)
    await upload(client, "a.png", original)
    await upload(client, "b.png", variant_resized(original))
    a, b = ids(db_conn)

    body = (await client.get(f"/api/files/{b}/similar")).json()
    assert body["file_id"] == b
    [item] = body["similar"]
    assert item["file_id"] == a
    assert item["status"] == "pending_review"
    assert item["thumb_url"] == f"/media/{a}/thumb"
    assert item["method"] == "image_phash"
    assert 0 <= item["distance"] <= 6
    assert item["dismissed"] is False
    assert (await client.get(f"/api/files/{a}/similar")).json()["similar"][0]["file_id"] == b

    response = await client.post(f"/api/similar/{b}/{a}/image_phash/dismiss")
    assert response.status_code == 200
    assert response.json() == {
        "file_a_id": a,
        "file_b_id": b,
        "method": "image_phash",
        "dismissed": True,
    }
    assert (await client.get(f"/api/files/{a}/similar")).json()["similar"][0]["dismissed"]

    assert (await client.post(f"/api/similar/{a}/{b}/video_frames/dismiss")).status_code == 404
    assert (await client.post(f"/api/similar/{a}/{b}/nope/dismiss")).status_code == 422
    assert (await client.get("/api/files/999999/similar")).status_code == 404


async def test_review_badges_and_file_page(client: httpx.AsyncClient, db_conn) -> None:
    first = structured_png(22)
    await upload(client, "first.png", first)
    await upload(client, "half.png", variant_resized(first))
    a, b = ids(db_conn)

    review = (await client.get(f"/review/fragments/grid?ids={a},{b}")).text
    assert "similar to archived or pending (1)" in review
    assert f'href="/files/{a}"' in review and f'href="/files/{b}"' in review
    assert "similar to discarded" not in review

    # Discard the first file, then upload a re-encode.
    discarded = await client.post("/api/review/discard", json={"file_ids": [a]})
    assert discarded.json()["ok"] is True
    await upload(client, "again.jpg", variant_jpeg(first))
    c = ids(db_conn)[-1]
    review = (await client.get(f"/review/fragments/grid?ids={b},{c}")).text
    tile = review[review.index(f'data-file-id="{c}"') :]
    tile = tile[: tile.index("</li>")]
    assert "similar to discarded (1)" in tile
    assert f'href="/files/{a}"' in tile
    assert "similar to archived or pending (1)" in tile  # half.png is pending
    assert 'class="badge badge-discarded"' in tile

    # An exact copy is still dropped.
    await upload(client, "copy.png", first)
    assert len(ids(db_conn)) == 3
    assert db_conn.execute(
        "SELECT outcome::text FROM ingest_events ORDER BY id DESC LIMIT 1"
    ).fetchone() == ("duplicate_exact",)

    page = (await client.get(f"/files/{c}")).text
    assert 'id="similar"' in page
    assert f'data-other-id="{a}"' in page and f'data-other-id="{b}"' in page
    assert "status-discarded" in page
    assert f'hx-post="/api/similar/{c}/{a}/image_phash/dismiss"' in page

    await client.post(f"/api/similar/{a}/{c}/image_phash/dismiss")
    review = (await client.get(f"/review/fragments/grid?ids={c}")).text
    tile = review[review.index(f'data-file-id="{c}"') :]
    assert "similar to discarded" not in tile[: tile.index("</li>")]
    page = (await client.get(f"/files/{c}")).text
    assert 'class="similar-item dismissed"' in page


async def test_file_page_without_similar_files(client: httpx.AsyncClient, db_conn) -> None:
    await upload(client, "solo.png", structured_png(23))
    (only,) = ids(db_conn)
    assert "No similar files." in (await client.get(f"/files/{only}")).text
