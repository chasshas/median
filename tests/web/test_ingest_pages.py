import httpx
import psycopg
import pytest

from app.jobs import run_queued_jobs
from tests.media import make_video, png_bytes

pytestmark = pytest.mark.usefixtures("clean_ingest")


async def _upload(client: httpx.AsyncClient, files) -> None:
    response = await client.post(
        "/api/uploads", files=[("files", (n, d, "application/octet-stream")) for n, d in files]
    )
    assert response.status_code == 202
    await run_queued_jobs()


async def test_upload_page(client: httpx.AsyncClient) -> None:
    html = (await client.get("/upload")).text
    assert 'id="upload-form"' in html
    assert 'name="files" multiple' in html
    assert 'name="tags"' in html
    assert 'name="force"' in html
    assert "js/upload.js" in html


async def test_review_dashboard_and_file_page(
    client: httpx.AsyncClient, db_conn: psycopg.Connection, tmp_path
) -> None:
    video = make_video(tmp_path / "v.mp4").read_bytes()
    images = [(f"img{i}.png", png_bytes((i * 60, 10, 10))) for i in range(3)]
    await _upload(client, [*images, ("v.mp4", video)])

    ids = [r[0] for r in db_conn.execute("SELECT id FROM files ORDER BY id").fetchall()]
    assert len(ids) == 4
    review = (await client.get(f"/review/fragments/grid?ids={','.join(map(str, ids))}")).text
    for file_id in ids:
        assert f'src="/media/{file_id}/thumb"' in review

    dashboard = (await client.get("/")).text
    assert 'id="count-pending_review">4<' in dashboard
    assert 'id="count-error">0<' in dashboard

    (video_id,) = db_conn.execute("SELECT id FROM files WHERE media_kind = 'video'").fetchone()
    page = (await client.get(f"/files/{video_id}")).text
    assert f'<video src="/media/{video_id}/original" controls' in page
    assert 'id="file-status">pending review<' in page
    assert "upload" in page
