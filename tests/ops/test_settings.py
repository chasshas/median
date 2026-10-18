from datetime import UTC, datetime

import psycopg
import pytest

from app.dedup.settings import DedupSettings, get_dedup_settings
from app.ops import settings as ops_settings
from app.ops.settings import SettingsError, load_settings, parse_form, save_settings
from app.web import localtime
from tests.ingest.helpers import run
from tests.media import structured_png, variant_jpeg

pytestmark = pytest.mark.usefixtures("clean_ingest")


async def test_database_values_override_environment_defaults(sessionmaker, db_conn) -> None:
    async with sessionmaker() as session:
        before = await load_settings(session)
        assert before.image_phash_threshold == 6
        assert before.backup_retention == 14
        assert before.review_order == "oldest"
        assert before.tools_update_schedule == "0 5 * * 1"
        assert before.overridden == frozenset()

        changed = await save_settings(
            session,
            {
                "image_phash_threshold": 2,
                "app_timezone": "UTC",
                "backup_retention": 3,
                "review_order": "newest",
                "tools_update_schedule": "15  2 * * 3",
            },
        )
    assert changed == {
        "image_phash_threshold",
        "app_timezone",
        "backup_retention",
        "review_order",
        "tools_update_schedule",
    }
    rows = dict(db_conn.execute("SELECT key, value FROM app_settings").fetchall())
    assert rows["image_phash_threshold"] == 2
    assert rows["tools_update_schedule"] == "15 2 * * 3"  # whitespace normalized
    current = ops_settings.current()
    assert current.image_phash_threshold == 2
    assert current.video_frame_threshold == 8  # not overridden: environment default
    assert current.overridden >= {"image_phash_threshold", "app_timezone"}
    assert localtime(datetime(2026, 1, 1, 12, 0, tzinfo=UTC)) == "2026-01-01 12:00:00 UTC"

    # Back to the default value: the override row goes away.
    async with sessionmaker() as session:
        await save_settings(session, {"image_phash_threshold": 6, "app_timezone": "Asia/Seoul"})
    keys = {r[0] for r in db_conn.execute("SELECT key FROM app_settings").fetchall()}
    assert "image_phash_threshold" not in keys and "app_timezone" not in keys
    assert localtime(datetime(2026, 1, 1, 12, 0, tzinfo=UTC)) == "2026-01-01 21:00:00 KST"


async def test_invalid_values_are_refused(sessionmaker) -> None:
    async with sessionmaker() as session:
        await load_settings(session)
    with pytest.raises(SettingsError) as err:
        parse_form(
            {
                "image_phash_threshold": "65",
                "app_timezone": "Mars/Olympus",
                "tools_update_schedule": "every monday",
                "backup_retention": "0",
                "review_order": "random",
            }
        )
    assert set(err.value.errors) == {
        "image_phash_threshold",
        "app_timezone",
        "tools_update_schedule",
        "backup_retention",
        "review_order",
    }
    assert parse_form({"video_frame_threshold": " 10 ", "unknown": "x"}) == {
        "video_frame_threshold": 10
    }


async def test_invalid_stored_value_falls_back(sessionmaker, db_conn) -> None:
    db_conn.execute("INSERT INTO app_settings (key, value) VALUES ('app_timezone', '\"Nope/X\"')")
    async with sessionmaker() as session:
        assert (await load_settings(session)).app_timezone == "Asia/Seoul"


async def test_dedup_uses_the_override(sessionmaker, paths, db_conn: psycopg.Connection) -> None:
    async with sessionmaker() as session:
        await save_settings(session, {"image_phash_threshold": 0})
        assert await get_dedup_settings(session) == DedupSettings(0, 8)
    original = structured_png(5)
    variant = variant_jpeg(original)
    await run(sessionmaker, paths, original, "a.png")
    await run(sessionmaker, paths, variant, "b.jpg")
    distance = db_conn.execute(
        "SELECT bit_count((a.phash # b.phash)::bit(64)) FROM files a, files b"
        " WHERE a.original_filename = 'a.png' AND b.original_filename = 'b.jpg'"
    ).fetchone()[0]
    assert distance > 0, "the variant must differ for this test to mean anything"
    assert db_conn.execute("SELECT count(*) FROM similar_pairs").fetchone() == (0,)

    async with sessionmaker() as session:
        await save_settings(session, {"image_phash_threshold": 6})
    await run(sessionmaker, paths, variant_jpeg(original, quality=50), "c.jpg")
    assert db_conn.execute("SELECT count(*) FROM similar_pairs").fetchone()[0] >= 1


async def test_settings_page_saves_and_offers_recompute(client, db_conn) -> None:
    page = await client.get("/settings")
    assert page.status_code == 200
    assert 'name="image_phash_threshold" value="6"' in page.text
    assert 'data-run="backup"' in page.text and 'data-run="integrity"' in page.text

    bad = await client.post("/settings", data={"image_phash_threshold": "99"})
    assert bad.status_code == 400
    assert 'data-error="image_phash_threshold"' in bad.text

    saved = await client.post("/settings", data={"review_order": "newest"})
    assert saved.status_code == 303 and saved.headers["location"] == "/settings?notice=saved"

    changed = await client.post("/settings", data={"video_frame_threshold": "5"})
    assert changed.headers["location"] == "/settings?notice=thresholds"
    page = await client.get(changed.headers["location"])
    assert 'id="recompute-similar"' in page.text
    assert 'data-overridden="video_frame_threshold"' in page.text

    run = await client.post("/settings/run/recompute_similar")
    assert run.status_code == 303
    run_id = int(run.headers["location"].rsplit("/", 1)[1])
    row = db_conn.execute(
        "SELECT kind::text, status::text, queue_job_id IS NOT NULL FROM job_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    assert row == ("recompute_similar", "queued", True)
    again = await client.post("/settings/run/recompute_similar")
    assert again.headers["location"] == f"/jobs/{run_id}?notice=already-active"
    assert (await client.post("/settings/run/crawl")).status_code == 404


async def test_review_queue_default_order_comes_from_settings(
    client, sessionmaker, paths, db_conn
) -> None:
    await run(sessionmaker, paths, structured_png(1), "first.png")
    await run(sessionmaker, paths, structured_png(2), "second.png")
    db_conn.execute(
        "UPDATE files SET collected_at = now() - interval '1 hour'"
        " WHERE original_filename = 'first.png'"
    )
    names = [
        i["original_filename"] for i in (await client.get("/api/review/queue")).json()["items"]
    ]
    assert names == ["first.png", "second.png"]
    await client.post("/settings", data={"review_order": "newest"})
    names = [
        i["original_filename"] for i in (await client.get("/api/review/queue")).json()["items"]
    ]
    assert names == ["second.png", "first.png"]
    page = await client.get("/review")
    assert '<option value="newest" selected>' in page.text
