"""Rendering of /library, /tags and the file page's folder, tag editor and prev/next links."""

import re

import httpx
import psycopg
import pytest

from tests.library.helpers import fake_archived, make_folder, set_tag, tag_id
from tests.review.helpers import inbox

pytestmark = pytest.mark.usefixtures("clean_ingest")


def folder_link(html: str, folder_id: int) -> str:
    match = re.search(rf'<a href="/library\?folder={folder_id}"[^>]*>(.*?)</a>', html, re.S)
    assert match, f"no tree link for folder {folder_id}"
    return re.sub(r"<[^>]+>", "", match.group(1)).split()


async def test_library_tree_grid_and_controls(client: httpx.AsyncClient, db_conn) -> None:
    box = inbox(db_conn)
    art = make_folder(db_conn, "Art")
    sketches = make_folder(db_conn, "Sketches", art)
    fake_archived(db_conn, box, 1, "i")
    fake_archived(db_conn, art, 2, "a")
    in_sketches = fake_archived(db_conn, sketches, 3, "s")

    html = (await client.get("/library")).text
    assert "<h1>All folders</h1>" in html and "js/library.js" in html
    assert folder_link(html, box) == ["Inbox", "1"]
    assert folder_link(html, art) == ["Art", "2", "/", "5"]  # direct / with subfolders
    assert folder_link(html, sketches) == ["Sketches", "3"]
    assert html.index(">Inbox") < html.index(">Art")
    assert html.count('class="tile library-tile"') == 6

    page = (await client.get(f"/library?folder={art}")).text
    assert "<h1>Art</h1>" in page
    assert page.count('class="tile library-tile"') == 2
    assert "subfolders have 5" not in page
    assert 'data-folder-action="rename"' in page and 'data-folder-action="delete"' in page
    # The move target list leaves out the folder itself and its subfolders.
    move = page[page.index('data-folder-action="move"') :]
    move = move[: move.index("</form>")]
    assert f'value="{sketches}"' not in move and f'value="{box}"' in move

    with_sub = (await client.get(f"/library?folder={art}&sub=1&sort=name&dir=asc")).text
    assert with_sub.count('class="tile library-tile"') == 5
    names = re.findall(r'<span class="tile-name">([^<]+)</span>', with_sub)
    assert names == ["a0.png", "a1.png", "s0.png", "s1.png", "s2.png"]
    link = f'href="/files/{in_sketches[0]}?folder={art}&amp;sub=1&amp;sort=name&amp;from=library"'
    assert link in with_sub

    inbox_page = (await client.get(f"/library?folder={box}")).text
    assert 'data-folder-action="rename"' not in inbox_page  # the Inbox cannot change
    assert (await client.get("/library?folder=999999")).status_code == 404
    assert (await client.get("/library?folder=abc")).status_code == 400


async def test_library_pagination(client: httpx.AsyncClient, db_conn) -> None:
    folder = make_folder(db_conn, "Many")
    fake_archived(db_conn, folder, 61)
    first = (await client.get(f"/library?folder={folder}")).text
    assert first.count('class="tile library-tile"') == 60
    assert "Page 1 of 2" in first and f'href="/library?folder={folder}&amp;page=2"' in first
    second = (await client.get(f"/library?folder={folder}&page=2")).text
    assert second.count('class="tile library-tile"') == 1 and 'rel="prev"' in second


async def test_tags_page(client: httpx.AsyncClient, db_conn) -> None:
    a, b = fake_archived(db_conn, inbox(db_conn), 2)
    set_tag(db_conn, a, "cat", "user")
    set_tag(db_conn, b, "cat", "crawler")
    set_tag(db_conn, b, "dog", "crawler", removed=True)

    html = (await client.get("/tags")).text
    assert "js/tags.js" in html
    rows = re.findall(r'data-tag-name="([^"]+)" data-count="(\d+)"', html)
    assert rows == [("cat", "2"), ("dog", "0")]
    filtered = (await client.get("/tags?q=DO")).text
    assert re.findall(r'data-tag-name="([^"]+)"', filtered) == ["dog"]
    assert "No tag contains" in (await client.get("/tags?q=zzz")).text
    assert 'aria-current="page">Tags' in html and "Coming in phase" not in html


async def test_file_page_tags_folder_and_neighbors(
    client: httpx.AsyncClient, db_conn: psycopg.Connection
) -> None:
    folder = make_folder(db_conn, "Trips")
    nested = make_folder(db_conn, "Coast", folder)
    ids = fake_archived(db_conn, nested, 3)
    for i, file_id in enumerate(ids):
        db_conn.execute(
            "UPDATE files SET archived_at = timestamptz '2026-05-01Z' + %s * interval '1 day'"
            " WHERE id = %s",
            (i, file_id),
        )
    middle = ids[1]
    set_tag(db_conn, middle, "beach", "crawler")
    set_tag(db_conn, middle, "mine", "user")
    set_tag(db_conn, middle, "gone", "crawler", removed=True)

    html = (await client.get(f"/files/{middle}?folder={folder}&sub=1&from=library")).text
    assert '<dd id="file-folder">Trips / Coast</dd>' in html
    assert f'<option value="{nested}" selected>Trips / Coast</option>' in html
    assert re.search(rf'data-tag-id="{tag_id(db_conn, "beach")}" title="crawler tag"', html)
    assert 'class="tag chip tag-user"' in html
    assert "Removed tags (1)" in html and f'data-tag-restore="{tag_id(db_conn, "gone")}"' in html
    # Newest archived first: ids[2], ids[1], ids[0].
    assert f'href="/files/{ids[2]}?folder={folder}&amp;sub=1&amp;from=library" rel="prev"' in html
    assert f'href="/files/{ids[0]}?folder={folder}&amp;sub=1&amp;from=library" rel="next"' in html
    assert "2 of 3 in the library" in html
    assert f'href="/library?folder={folder}&amp;sub=1"' in html

    last = (await client.get(f"/files/{ids[0]}?folder={folder}&sub=1&from=library")).text
    assert 'rel="next"' not in last and "3 of 3" in last
    # Without the library context (or outside it) there are no arrows.
    assert 'class="file-nav"' not in (await client.get(f"/files/{middle}")).text
    outside = (await client.get(f"/files/{middle}?folder={folder}&from=library")).text
    assert 'class="file-nav"' not in outside

    fragment = (await client.get(f"/files/{middle}/fragments/tags")).text
    assert 'id="file-tag-editor"' in fragment and "<html" not in fragment
    assert (await client.get("/files/999999/fragments/tags")).status_code == 404
