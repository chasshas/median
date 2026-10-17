"""Rendering of /search: query box, filter panel in sync with the query, results grid, saved
searches, redirects that keep the URL in step with the query, and the nav search box."""

import re
from datetime import UTC, datetime
from html import unescape
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.library.helpers import make_folder, set_tag
from tests.search.helpers import add_file

pytestmark = pytest.mark.usefixtures("clean_ingest")


def tile_ids(html: str) -> list[int]:
    return [
        int(i) for i in re.findall(r'class="tile library-tile[^"]*" data-file-id="(\d+)"', html)
    ]


def field(html: str, name: str) -> str:
    match = re.search(rf'<input type="(?:text|date)" name="{name}" value="([^"]*)"', html)
    assert match, f"no panel field {name}"
    return match.group(1)


def checked(html: str, name: str) -> list[str]:
    return re.findall(rf'name="{name}" value="([^"]+)" checked', html)


async def test_query_box_panel_and_results(client: httpx.AsyncClient, db_conn) -> None:
    animals = make_folder(db_conn, "Animals")
    cats = make_folder(db_conn, "Cats", animals)
    in_2026 = datetime(2026, 5, 1, tzinfo=UTC)
    cat = add_file(db_conn, folder_id=cats, name="cat.png", collected_at=in_2026)
    dog = add_file(db_conn, folder_id=animals, name="dog.png", collected_at=in_2026)
    old = add_file(
        db_conn, folder_id=animals, name="old.png", collected_at=datetime(2025, 5, 1, tzinfo=UTC)
    )
    vid = add_file(db_conn, folder_id=animals, media_kind="video", collected_at=in_2026)
    for f in (cat, dog, old, vid):
        set_tag(db_conn, f, "cat", "user")
    set_tag(db_conn, dog, "dog", "user")

    query = 'cat -dog folder:"Animals" collected:2026-01-01..2026-12-31 type:image'
    response = await client.get("/search", params={"q": query})
    assert response.status_code == 200
    html = response.text
    assert tile_ids(html) == [cat]
    shown = query.replace('"', "&#34;")
    assert f'id="search-q" value="{shown}"' in html
    # The filter panel shows the same conditions.
    assert field(html, "tags_all") == "cat"
    assert field(html, "tags_none") == "dog"
    assert field(html, "tags_any") == ""
    assert field(html, "collected_from") == "2026-01-01"
    assert field(html, "collected_to") == "2026-12-31"
    assert re.search(rf'<option value="{animals}" selected>Animals</option>', html)
    assert 'name="exact" value="1" checked' not in html
    assert checked(html, "type") == ["image"]
    assert checked(html, "status") == ["archived"]
    assert "Animals / Cats" in html  # folder path on the tile
    assert 'id="selection-bar"' in html and "js/search.js" in html
    assert f'data-query="{shown}"' in html


async def test_panel_submit_redirects_to_the_query(client: httpx.AsyncClient, db_conn) -> None:
    animals = make_folder(db_conn, "Animals")
    params = {
        "panel": "1",
        "sort": "size_asc",
        "tags_all": "cat  Big Cat",
        "tags_none": "dog",
        "folder": str(animals),
        "exact": "1",
        "collected_from": "2026-01-01",
        "collected_to": "",
        "type": ["image", "video"],
        "status": ["archived", "pending"],
        "text": ["hello world", ""],
    }
    response = await client.get("/search", params=params)
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/search"
    got = parse_qs(location.query)
    assert got["q"] == [
        'cat big -dog folder=:"Animals" collected:2026-01-01.. type:image type:video'
        ' status:archived status:pending "hello world"'
    ]
    assert got["sort"] == ["size_asc"]

    empty = await client.get("/search", params={"panel": "1", "status": "archived"})
    assert empty.status_code == 303 and empty.headers["location"] == "/search?q="


async def test_random_sort_gets_a_seed_in_the_url(client: httpx.AsyncClient, db_conn) -> None:
    for _ in range(3):
        add_file(db_conn)
    response = await client.get("/search", params={"q": "", "sort": "random"})
    assert response.status_code == 303
    got = parse_qs(urlsplit(response.headers["location"]).query)
    seed = got["seed"][0]
    page1 = await client.get("/search", params={"q": "", "sort": "random", "seed": seed})
    page2 = await client.get("/search", params={"q": "", "sort": "random", "seed": seed})
    assert tile_ids(page1.text) == tile_ids(page2.text) and len(tile_ids(page1.text)) == 3
    assert f'name="seed" value="{seed}"' in page1.text


async def test_syntax_errors_and_warnings(client: httpx.AsyncClient) -> None:
    html = unescape((await client.get("/search", params={"q": "cat type:picture"})).text)
    assert 'unknown type "picture" (use image|video|audio|other) (at character 10)' in html
    assert 'id="library-grid"' not in html
    html = unescape((await client.get("/search", params={"q": "ghost"})).text)
    assert 'no file has the tag "ghost"' in html
    assert "No files match." in html


async def test_pagination_links(client: httpx.AsyncClient, db_conn) -> None:
    ids = [add_file(db_conn) for _ in range(5)]
    html = (await client.get("/search", params={"q": "", "limit": 2})).text
    assert len(tile_ids(html)) == 2 and "showing 1\u20132" in html
    seen = tile_ids(html)
    while True:
        match = re.search(r'<a href="([^"]+)" rel="next">', html)
        if not match:
            break
        html = (await client.get(match.group(1).replace("&amp;", "&") + "&limit=2")).text
        assert 'rel="first"' in html
        seen += tile_ids(html)
    assert sorted(seen) == sorted(ids)


async def test_discarded_tiles_and_deleted_originals(
    client: httpx.AsyncClient, db_conn, paths
) -> None:
    kept = add_file(db_conn, status="discarded", name="kept.png")
    purged = add_file(db_conn, status="discarded", name="purged.png")
    sha, ext = db_conn.execute("SELECT sha256, ext FROM files WHERE id = %s", (kept,)).fetchone()
    trash = paths.trash_path(sha.hex(), ext)
    trash.parent.mkdir(parents=True, exist_ok=True)
    trash.write_bytes(b"x")
    html = (await client.get("/search", params={"q": "status:discarded"})).text
    assert sorted(tile_ids(html)) == sorted([kept, purged])
    tiles = dict(re.findall(r'data-file-id="(\d+)" data-status="discarded">(.*?)</li>', html, re.S))
    assert "original deleted" in tiles[str(purged)]
    assert "original deleted" not in tiles[str(kept)]
    assert 'class="tile library-tile status-discarded"' in html


async def test_saved_searches_sidebar(client: httpx.AsyncClient) -> None:
    await client.post("/api/saved-searches", json={"name": "Cats", "query": "cat type:image"})
    html = (await client.get("/search", params={"q": "cat type:image"})).text
    assert '<a href="/search?q=cat+type%3Aimage" class="folder-link current"' in html
    assert "data-saved-rename" in html and "data-saved-delete" in html


async def test_nav_search_box_on_every_page(client: httpx.AsyncClient) -> None:
    for path in ("/", "/library", "/tags"):
        html = (await client.get(path)).text
        assert '<form method="get" action="/search" class="nav-search" role="search">' in html
    html = (await client.get("/search", params={"q": "Cat"})).text
    assert 'aria-label="Search the library" autocomplete="off" value="cat"' in html
