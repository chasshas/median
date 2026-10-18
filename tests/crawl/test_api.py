import httpx
import pytest

from app.jobs import run_queued_jobs
from tests.conftest import SAME_ORIGIN
from tests.crawl.helpers import run_status


@pytest.fixture
async def client(crawl_app):
    transport = httpx.ASGITransport(app=crawl_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=SAME_ORIGIN
    ) as http:
        yield http


async def create_demo(client, **extra) -> dict:
    body = {"name": "Demo", "addon_key": "demo", "config": {"count": 3, "seed": 5}, **extra}
    reply = await client.post("/api/sources", json=body)
    assert reply.status_code == 201, reply.text
    return reply.json()


async def test_source_crud(client, db_conn) -> None:
    source = await create_demo(client, default_tags=["Night Sky"], schedule="0 * * * *")
    assert source["config"] == {"count": 3, "seed": 5, "tags": [], "delay_seconds": 0.0}
    assert source["default_tags"] == ["night_sky"]
    assert source["last_run"] is None

    listing = (await client.get("/api/sources")).json()
    assert [s["id"] for s in listing["sources"]] == [source["id"]]
    demo = next(a for a in listing["addons"] if a["key"] == "demo")
    assert [f["name"] for f in demo["fields"]] == ["count", "seed", "tags", "delay_seconds"]

    bad = await client.post(
        "/api/sources", json={"name": "x", "addon_key": "demo", "config": {"count": 0}}
    )
    assert bad.status_code == 400
    assert "count" in bad.json()["detail"]["fields"]
    unknown = await client.post("/api/sources", json={"name": "x", "addon_key": "nope"})
    assert unknown.status_code == 400
    folder = await client.post(
        "/api/sources", json={"name": "x", "addon_key": "demo", "default_folder_id": 999}
    )
    assert folder.status_code == 400

    updated = await client.put(
        f"/api/sources/{source['id']}",
        json={"name": "Renamed", "config": {"count": 4}, "enabled": False},
    )
    assert updated.status_code == 200
    assert (updated.json()["name"], updated.json()["config"]["count"]) == ("Renamed", 4)
    assert updated.json()["addon_key"] == "demo"
    enabled = await client.patch(f"/api/sources/{source['id']}", json={"enabled": True})
    assert enabled.json()["enabled"] is True

    assert (await client.delete(f"/api/sources/{source['id']}")).status_code == 204
    assert (await client.get(f"/api/sources/{source['id']}")).status_code == 404


async def test_run_now_and_jobs(client, db_conn) -> None:
    source = await create_demo(client)
    run = await client.post(f"/api/sources/{source['id']}/run")
    assert run.status_code == 202
    run_id = run.json()["run_id"]
    again = await client.post(f"/api/sources/{source['id']}/run")
    assert again.status_code == 409
    assert again.json()["detail"]["run_id"] == run_id
    blocked = await client.delete(f"/api/sources/{source['id']}")
    assert blocked.status_code == 409

    await run_queued_jobs()
    assert run_status(db_conn, run_id) == "succeeded"
    runs = (await client.get("/api/jobs")).json()["runs"]
    assert [r["id"] for r in runs] == [run_id]
    assert runs[0]["source_name"] == "Demo"
    assert runs[0]["stats"]["new"] == 3
    assert runs[0]["duration_seconds"] >= 0
    log = await client.get(f"/api/jobs/{run_id}/log")
    assert "emitted 3 images" in log.text
    assert (await client.get("/api/jobs/999")).status_code == 404
    listing = (await client.get("/api/sources")).json()["sources"][0]
    assert listing["last_run"]["status"] == "succeeded"

    # Deleting the source keeps the run and the files.
    assert (await client.delete(f"/api/sources/{source['id']}")).status_code == 204
    assert (await client.get(f"/api/jobs/{run_id}")).json()["source_id"] is None
    assert db_conn.execute("SELECT count(*) FROM files").fetchone()[0] == 3


async def test_pages(client, db_conn) -> None:
    source = await create_demo(client, schedule="*/5 * * * *")
    page = await client.get("/sources")
    assert page.status_code == 200
    assert "<code>*/5 * * * *</code>" in page.text
    assert f'action="/sources/{source["id"]}/run"' in page.text
    assert "never" in page.text

    run = await client.post(f"/sources/{source['id']}/run")
    assert run.status_code == 303
    run_url = run.headers["location"]
    run_id = int(run_url.rsplit("/", 1)[1])
    again = await client.post(f"/sources/{source['id']}/run")
    assert again.headers["location"] == f"/jobs/{run_id}?notice=already-active"

    detail = await client.get(run_url)
    assert detail.status_code == 200
    assert 'hx-trigger="every 2s"' in detail.text
    assert 'id="cancel-run"' in detail.text
    jobs = await client.get("/jobs")
    assert f'data-run-id="{run_id}"' in jobs.text

    await run_queued_jobs()
    live = await client.get(f"/jobs/{run_id}/fragments/live")
    assert 'data-status="succeeded"' in live.text
    assert "hx-trigger" not in live.text  # polling stops once the run is over
    assert 'data-stat="new">3<' in live.text
    assert "emitted 3 images" in live.text
    page = await client.get("/sources")
    assert f'href="/jobs/{run_id}" class="job-status job-succeeded"' in page.text

    toggle = await client.post(f"/sources/{source['id']}/toggle")
    assert toggle.status_code == 303
    assert db_conn.execute("SELECT enabled FROM crawl_sources").fetchone()[0] is False

    cancel = await client.post(f"/jobs/{run_id}/cancel")
    assert cancel.headers["location"] == f"/jobs/{run_id}?notice=not-active"
    delete = await client.post(f"/sources/{source['id']}/delete")
    assert delete.headers["location"] == "/sources?notice=deleted"
