import httpx
import psycopg
import pytest

from app.jobs import run_queued_jobs
from tests.media import jpeg_bytes, make_video, png_bytes

pytestmark = pytest.mark.usefixtures("clean_ingest")


def multipart(files: list[tuple[str, bytes]], **fields: str):
    return {
        "files": [("files", (name, data, "application/octet-stream")) for name, data in files],
        "data": fields,
    }


async def upload(client: httpx.AsyncClient, files, **fields) -> dict:
    response = await client.post("/api/uploads", **multipart(files, **fields))
    assert response.status_code == 202, response.text
    return response.json()


async def test_upload_end_to_end_with_duplicate(
    client: httpx.AsyncClient, db_conn: psycopg.Connection, paths
) -> None:
    red, blue = png_bytes((255, 0, 0)), jpeg_bytes((0, 0, 255))
    body = await upload(
        client, [("red.png", red), ("blue.jpg", blue), ("red-again.png", red)], tags="Trip, 여행"
    )
    assert body["files"] == 3

    pending = (await client.get(f"/api/uploads/{body['batch_id']}")).json()
    assert pending["pending"] == 3 and pending["done"] is False

    await run_queued_jobs()

    status = (await client.get(f"/api/uploads/{body['batch_id']}")).json()
    assert status["done"] is True
    assert status["counts"]["new"] == 2
    assert status["counts"]["duplicate_exact"] == 1
    assert [f["outcome"] for f in status["files"]] == ["new", "new", "duplicate_exact"]
    assert db_conn.execute("SELECT count(*) FROM files").fetchone() == (2,)
    tags = db_conn.execute(
        "SELECT DISTINCT t.name, ft.origin::text FROM file_tags ft JOIN tags t ON t.id = ft.tag_id"
    ).fetchall()
    assert set(tags) == {("trip", "user"), ("여행", "user")}  # no ORDER BY: collation-dependent
    staged = list(paths.staging_batch(body["batch_id"]).iterdir())
    assert len(staged) == 2  # the duplicate was deleted, no leftover .part files

    again = await upload(client, [("red.png", red), ("blue.jpg", blue)])
    await run_queued_jobs()
    second = (await client.get(f"/api/uploads/{again['batch_id']}")).json()
    assert second["counts"]["duplicate_exact"] == 2 and second["counts"]["new"] == 0


async def test_batch_status_htmx_fragment(client: httpx.AsyncClient) -> None:
    body = await upload(client, [("a.png", png_bytes((1, 1, 1)))])
    polling = await client.get(f"/api/uploads/{body['batch_id']}", headers={"HX-Request": "true"})
    assert 'hx-trigger="every 1s"' in polling.text

    await run_queued_jobs()
    done = await client.get(f"/api/uploads/{body['batch_id']}", headers={"HX-Request": "true"})
    assert 'data-batch-done="true"' in done.text
    assert 'data-outcome="new">1<' in done.text
    assert "hx-trigger" not in done.text


async def test_upload_force_flag(client: httpx.AsyncClient, db_conn: psycopg.Connection) -> None:
    data = png_bytes((9, 9, 9))
    await upload(client, [("a.png", data)])
    await run_queued_jobs()
    db_conn.execute("UPDATE files SET status = 'discarded'")
    body = await upload(client, [("a.png", data)], force="true")
    await run_queued_jobs()
    status = (await client.get(f"/api/uploads/{body['batch_id']}")).json()
    assert status["counts"]["forced_reingest"] == 1
    assert db_conn.execute("SELECT status::text FROM files").fetchone() == ("pending_review",)


async def test_upload_without_files_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/uploads", data={"tags": "x"}, files={"x": ("", b"")})
    assert response.status_code == 400
    response = await client.post(
        "/api/uploads", content=b"x", headers={"content-type": "text/plain"}
    )
    assert response.status_code == 400


async def test_unknown_batch(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/uploads/not-a-batch")).status_code == 404
    unknown = await client.get("/api/uploads/" + "0" * 32)
    assert unknown.json()["counts"]["new"] == 0


async def test_media_routes_and_range(
    client: httpx.AsyncClient, db_conn: psycopg.Connection, tmp_path
) -> None:
    video = make_video(tmp_path / "clip.mp4", seconds=2.0).read_bytes()
    await upload(client, [("clip.mp4", video)])
    await run_queued_jobs()
    (file_id,) = db_conn.execute("SELECT id FROM files").fetchone()

    full = await client.get(f"/media/{file_id}/original")
    assert full.status_code == 200
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-type"] == "video/mp4"
    assert full.content == video

    part = await client.get(f"/media/{file_id}/original", headers={"Range": "bytes=100-199"})
    assert part.status_code == 206
    assert part.headers["content-range"] == f"bytes 100-199/{len(video)}"
    assert part.content == video[100:200]

    tail = await client.get(f"/media/{file_id}/original", headers={"Range": "bytes=-50"})
    assert tail.status_code == 206
    assert tail.headers["content-range"] == f"bytes {len(video) - 50}-{len(video) - 1}/{len(video)}"

    thumb = await client.get(f"/media/{file_id}/thumb")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/webp"

    assert (await client.get("/media/999999/original")).status_code == 404


async def test_retry_api(
    client: httpx.AsyncClient, db_conn: psycopg.Connection, monkeypatch
) -> None:
    from app.ingest import process

    async def broken(path, name):
        raise RuntimeError("boom")

    monkeypatch.setattr(process, "probe", broken)
    await upload(client, [("a.png", png_bytes((3, 3, 3)))])
    await run_queued_jobs()
    (file_id, status) = db_conn.execute("SELECT id, status::text FROM files").fetchone()
    assert status == "error"
    review = await client.get("/review?tab=failed")
    assert f'hx-post="/api/files/{file_id}/retry"' in review.text

    monkeypatch.undo()
    response = await client.post(f"/api/files/{file_id}/retry")
    assert response.status_code == 202
    assert db_conn.execute("SELECT status::text FROM files").fetchone() == ("processing",)
    await run_queued_jobs()
    assert db_conn.execute("SELECT status::text FROM files").fetchone() == ("pending_review",)

    assert (await client.post(f"/api/files/{file_id}/retry")).status_code == 409
    assert (await client.post("/api/files/999999/retry")).status_code == 404
