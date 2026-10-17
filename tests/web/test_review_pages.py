"""Rendering of the review page and its single, grid and compare fragments."""

import httpx
import psycopg
import pytest

from app.ingest.origin import IngestOrigin
from tests.ingest.helpers import one, run
from tests.media import make_video, structured_png, variant_resized
from tests.review.helpers import pending

pytestmark = pytest.mark.usefixtures("clean_ingest")


def ids_by_name(conn: psycopg.Connection) -> dict[str, int]:
    return dict(conn.execute("SELECT original_filename, id FROM files").fetchall())


async def test_review_page_shell(client: httpx.AsyncClient, sessionmaker, paths, db_conn) -> None:
    await pending(sessionmaker, paths, db_conn, 2)
    db_conn.execute("INSERT INTO folders (parent_id, name) VALUES (NULL, 'Art')")

    html = (await client.get("/review?media_kind=image&order=newest")).text

    assert 'id="review"' in html
    assert 'data-queue-url="/api/review/queue?media_kind=image&amp;order=newest"' in html
    assert "js/review.js" in html
    assert 'id="remaining"' in html and 'id="decided"' in html
    assert '<option value="newest" selected>' in html
    assert 'value="image" checked' in html
    # Inbox is the first (default) archive folder.
    folders = html[html.index('id="archive-folder"') :]
    assert folders.index(">Inbox<") < folders.index(">Art<")
    for key in (
        "Shift</kbd>+<kbd>A",
        "<kbd>U</kbd>",
        "<kbd>C</kbd>",
        "<kbd>X</kbd>",
        "<kbd>S</kbd>",
    ):
        assert key in html
    assert 'aria-current="page">Pending' in html


async def test_failed_tab_lists_errors_with_retry(client: httpx.AsyncClient, db_conn) -> None:
    db_conn.execute(
        "INSERT INTO files"
        " (sha256, size_bytes, media_kind, status, error_message, original_filename)"
        " VALUES (decode(repeat('ab', 32), 'hex'), 1, 'other', 'error', 'probe failed', 'bad.jpg')"
    )
    (file_id,) = one(db_conn, "SELECT id FROM files")

    html = (await client.get("/review?tab=failed")).text

    assert 'id="failed"' in html and "probe failed" in html
    assert f'hx-post="/api/files/{file_id}/retry"' in html
    assert "js/review.js" not in html


async def test_single_fragment(client: httpx.AsyncClient, sessionmaker, paths, db_conn) -> None:
    original = structured_png(70)
    await run(sessionmaker, paths, original, "orig.png")
    origin = IngestOrigin(
        source_key="site", remote_id="r1", url="https://example.test/p/1", tags=["Cat Pic"]
    )
    await run(sessionmaker, paths, variant_resized(original), "half.png", origin)
    ids = ids_by_name(db_conn)
    await client.post("/api/review/discard", json={"file_ids": [ids["orig.png"]]})

    html = (await client.get(f"/review/fragments/single?file_id={ids['half.png']}")).text

    assert f'data-file-id="{ids["half.png"]}"' in html
    assert f'<img src="/media/{ids["half.png"]}/original"' in html
    assert "<dt>Dimensions</dt><dd>320 &times; 240</dd>" in html
    assert "<dt>Original name</dt><dd>half.png</dd>" in html
    assert 'href="https://example.test/p/1"' in html and "site · r1" in html
    assert 'class="tag tag-crawler"' in html and "cat_pic" in html
    # The similar strip shows the discarded match with its status.
    assert f'data-similar-ids="{ids["orig.png"]}"' in html
    strip = html[html.index('class="similar-strip"') :]
    assert f'data-compare-id="{ids["orig.png"]}"' in strip
    assert 'class="status status-discarded"' in strip
    assert (await client.get("/review/fragments/single?file_id=999999")).status_code == 404


async def test_single_fragment_media_kinds(
    client: httpx.AsyncClient, sessionmaker, paths, db_conn, tmp_path
) -> None:
    video = make_video(tmp_path / "clip.mp4").read_bytes()
    await run(sessionmaker, paths, video, "clip.mp4")
    await run(sessionmaker, paths, b"just some bytes", "notes.bin")
    ids = ids_by_name(db_conn)

    clip = (await client.get(f"/review/fragments/single?file_id={ids['clip.mp4']}")).text
    assert f'<video src="/media/{ids["clip.mp4"]}/original" controls' in clip
    assert "<dt>Duration</dt>" in clip
    other = (await client.get(f"/review/fragments/single?file_id={ids['notes.bin']}")).text
    assert 'class="file-card"' in other and "No similar files." in other


async def test_grid_fragment(client: httpx.AsyncClient, sessionmaker, paths, db_conn) -> None:
    original = structured_png(71)
    await run(sessionmaker, paths, original, "orig.png")
    await run(sessionmaker, paths, variant_resized(original), "half.png")
    await run(sessionmaker, paths, structured_png(72), "solo.png")
    ids = ids_by_name(db_conn)
    order = [ids["solo.png"], ids["half.png"], ids["orig.png"]]

    html = (await client.get(f"/review/fragments/grid?ids={','.join(map(str, order))},999999")).text

    positions = [html.index(f'data-file-id="{i}"') for i in order]
    assert positions == sorted(positions)
    assert html.count('class="tile review-tile"') == 3
    for file_id in order:
        assert f'src="/media/{file_id}/thumb"' in html
    half_tile = html[positions[1] : positions[2]]
    assert "similar to archived or pending (1)" in half_tile
    assert f'href="/files/{ids["orig.png"]}"' in half_tile
    assert "similar to" not in html[positions[0] : positions[1]]
    assert (await client.get("/review/fragments/grid?ids=1,abc")).status_code == 422


async def test_compare_fragment(client: httpx.AsyncClient, sessionmaker, paths, db_conn) -> None:
    original = structured_png(73)
    await run(sessionmaker, paths, original, "orig.png")
    await run(sessionmaker, paths, variant_resized(original), "half.png")
    ids = ids_by_name(db_conn)
    left, right = ids["half.png"], ids["orig.png"]

    html = (await client.get(f"/review/fragments/compare?left={left}&right={right}")).text

    assert f'data-left-id="{left}" data-right-id="{right}"' in html
    assert f'<img src="/media/{left}/original"' in html
    assert f'<img src="/media/{right}/original"' in html
    assert "320 &times; 240" in html and "640 &times; 480" in html
    # The larger resolution (the original, on the right) is highlighted.
    right_side = html[html.index(f'class="compare-side larger" data-file-id="{right}"') :]
    assert "larger resolution" in right_side
    assert html.count("compare-side larger") == 1
    assert "<dt>Format</dt><dd>png (image/png)</dd>" in html
    assert "image pHash, distance" in html
    assert 'data-choice="keep_left"' in html and 'data-choice="keep_right"' in html
    assert 'data-choice="replace"' not in html  # only when the right file is archived

    await client.post("/api/review/archive", json={"file_ids": [right]})
    archived = (await client.get(f"/review/fragments/compare?left={left}&right={right}")).text
    assert 'data-choice="replace"' in archived
    assert "<dt>Archived</dt>" in archived

    await client.post("/api/review/discard-archived", json={"file_ids": [right]})
    discarded = (await client.get(f"/review/fragments/compare?left={left}&right={right}")).text
    assert 'data-choice="keep_right" disabled' in discarded
    assert 'data-choice="keep_both" disabled' in discarded
    assert f'<img src="/media/{right}/original"' in discarded  # still in the trash

    assert (
        await client.get(f"/review/fragments/compare?left={left}&right={left}")
    ).status_code == 404
