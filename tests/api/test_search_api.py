"""GET /api/search (q or filter fields), parse, suggest, and saved searches CRUD."""

import httpx
import pytest

from tests.library.helpers import make_folder, set_tag
from tests.review.helpers import inbox
from tests.search.helpers import add_file, add_source

pytestmark = pytest.mark.usefixtures("clean_ingest")


@pytest.fixture
def animals(clean_ingest, db_conn) -> dict[str, int]:
    animals = make_folder(db_conn, "Animals")
    cats = make_folder(db_conn, "Cats", animals)
    ids = {
        "cat": add_file(db_conn, folder_id=animals, name="cat.png"),
        "kitten": add_file(db_conn, folder_id=cats, name="kitten.png", title="Kitten"),
        "both": add_file(db_conn, folder_id=cats, name="both.png"),
        "dogvid": add_file(db_conn, folder_id=animals, media_kind="video", name="dog.mp4"),
        "elsewhere": add_file(db_conn, folder_id=inbox(db_conn), name="other cat.png"),
        "gone": add_file(db_conn, status="discarded", name="old cat.png"),
    }
    for key in ("cat", "kitten", "both", "elsewhere", "gone"):
        set_tag(db_conn, ids[key], "cat", "user")
    set_tag(db_conn, ids["both"], "dog", "crawler")
    set_tag(db_conn, ids["dogvid"], "dog", "crawler")
    add_source(db_conn, ids["kitten"], "ytdlp")
    return ids | {"folder_animals": animals, "folder_cats": cats}


@pytest.mark.usefixtures("animals")
async def test_search_with_q(client: httpx.AsyncClient, animals) -> None:
    response = await client.get(
        "/api/search", params={"q": 'cat -dog folder:"animals" type:image ghost'}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0  # "ghost" matches nothing
    assert data["warnings"] == [{"message": 'no file has the tag "ghost"', "position": 37}]

    data = (await client.get("/api/search", params={"q": 'cat -dog folder:"Animals"'})).json()
    assert sorted(i["id"] for i in data["items"]) == sorted([animals["cat"], animals["kitten"]])
    assert data["query"] == 'cat -dog folder:"Animals"'
    item = next(i for i in data["items"] if i["id"] == animals["kitten"])
    assert item["folder_path"] == "Animals / Cats"
    assert item["tags"] == ["cat"]
    assert item["status"] == "archived" and item["media_kind"] == "image"
    assert item["thumb_url"] is None  # no thumbnail for these rows

    data = (await client.get("/api/search", params={"q": "cat status:discarded"})).json()
    assert [i["id"] for i in data["items"]] == [animals["gone"]]
    assert data["items"][0]["original_deleted"] is True  # no trash copy on disk


async def test_search_with_fields(client: httpx.AsyncClient, animals) -> None:
    params = {
        "tags_all": "cat",
        "tags_none": "dog",
        "folder": str(animals["folder_animals"]),
        "type": "image",
        "source": "ytdlp",
    }
    data = (await client.get("/api/search", params=params)).json()
    assert [i["id"] for i in data["items"]] == [animals["kitten"]]
    assert data["query"] == 'cat -dog folder:"Animals" type:image source:ytdlp'
    exact = (await client.get("/api/search", params=params | {"exact": "1"})).json()
    assert exact["total"] == 0
    bad = await client.get("/api/search", params={"collected_from": "yesterday"})
    assert bad.status_code == 400
    bad = await client.get("/api/search", params={"folder": "999999"})
    assert bad.status_code == 400


@pytest.mark.usefixtures("animals")
async def test_pages_and_errors(client: httpx.AsyncClient) -> None:
    first = (await client.get("/api/search", params={"q": "", "limit": 4})).json()
    assert first["total"] == 5 and len(first["items"]) == 4 and first["next_cursor"]
    second = (
        await client.get(
            "/api/search", params={"q": "", "limit": 4, "cursor": first["next_cursor"]}
        )
    ).json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    assert not {i["id"] for i in first["items"]} & {i["id"] for i in second["items"]}

    random = (await client.get("/api/search", params={"q": "", "sort": "random"})).json()
    assert isinstance(random["seed"], int)

    response = await client.get("/api/search", params={"q": 'cat "open'})
    assert response.status_code == 400
    assert response.json()["detail"] == {"message": "unterminated quote", "position": 4}
    response = await client.get("/api/search", params={"q": "", "cursor": "zzz"})
    assert response.status_code == 400


async def test_parse_and_suggest(client: httpx.AsyncClient, animals) -> None:
    data = (await client.get("/api/search/parse", params={"q": "Cat folder=:animals/cats"})).json()
    assert data["query"] == 'cat folder=:"Animals/Cats"'
    assert data["fields"]["folder"] == str(animals["folder_cats"])
    assert data["fields"]["exact"] is True
    assert data["fields"]["tags_all"] == "cat"
    assert data["fields"]["status"] == ["archived"]

    async def suggest(token: str) -> list[str]:
        response = await client.get("/api/search/suggest", params={"token": token})
        return [s["value"] for s in response.json()["suggestions"]]

    assert await suggest("ca") == ["cat"]
    assert await suggest("-d") == ["-dog"]
    assert await suggest("~c") == ["~cat"]
    assert await suggest("fo") == ["folder:", "folder=:"]
    assert await suggest("folder:ca") == ['folder:"Animals/Cats"']
    assert await suggest('folder=:"an') == ['folder=:"Animals"', 'folder=:"Animals/Cats"']
    assert await suggest("type:v") == ["type:video"]
    assert await suggest("status:") == ["status:pending", "status:archived", "status:discarded"]
    assert await suggest("source:y") == ["source:ytdlp"]
    assert "collected:" in await suggest("")


async def test_saved_searches_crud(client: httpx.AsyncClient, animals) -> None:
    created = await client.post(
        "/api/saved-searches", json={"name": "  My  cats ", "query": 'cat folder:"Animals"'}
    )
    assert created.status_code == 201
    saved = created.json()
    assert saved["name"] == "My cats" and saved["query"] == 'cat folder:"Animals"'

    dup = await client.post("/api/saved-searches", json={"name": "my cats", "query": "x"})
    assert dup.status_code == 201  # names are unique as typed (case-sensitive)
    same = await client.post("/api/saved-searches", json={"name": "My cats", "query": "x"})
    assert same.status_code == 409
    bad = await client.post("/api/saved-searches", json={"name": "Bad", "query": "type:nope"})
    assert bad.status_code == 400 and bad.json()["detail"]["position"] == 5
    empty = await client.post("/api/saved-searches", json={"name": "  ", "query": "cat"})
    assert empty.status_code == 400

    listed = (await client.get("/api/saved-searches")).json()["saved_searches"]
    assert "My cats" in [s["name"] for s in listed]
    got = (await client.get(f"/api/saved-searches/{saved['id']}")).json()
    assert got == saved

    renamed = await client.patch(f"/api/saved-searches/{saved['id']}", json={"name": "Cats"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Cats"
    changed = await client.patch(f"/api/saved-searches/{saved['id']}", json={"query": "dog"})
    assert changed.json()["query"] == "dog" and changed.json()["name"] == "Cats"
    assert (
        await client.patch(f"/api/saved-searches/{saved['id']}", json={"query": '"x'})
    ).status_code == 400

    # The stored query reopens with the same results.
    reopened = (await client.get("/api/search", params={"q": changed.json()["query"]})).json()
    assert sorted(i["id"] for i in reopened["items"]) == sorted(
        [animals["both"], animals["dogvid"]]
    )

    assert (await client.delete(f"/api/saved-searches/{saved['id']}")).json() == {"ok": True}
    assert (await client.get(f"/api/saved-searches/{saved['id']}")).status_code == 404
    assert (await client.delete(f"/api/saved-searches/{saved['id']}")).status_code == 404
