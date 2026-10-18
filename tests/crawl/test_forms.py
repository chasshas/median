import re
import stat

import httpx
import pytest

from app.crawl import service
from app.crawl.forms import FormError, form_fields, parse_config_form
from tests.conftest import SAME_ORIGIN
from tests.crawl.addons import EveryFieldConfig


@pytest.fixture
async def client(crawl_app):
    transport = httpx.ASGITransport(app=crawl_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=SAME_ORIGIN
    ) as http:
        yield http


def test_field_kinds_from_schema() -> None:
    fields = {f.name: f for f in form_fields(EveryFieldConfig)}
    assert {n: f.kind for n, f in fields.items()} == {
        "title": "string",
        "limit": "integer",
        "ratio": "number",
        "verbose": "boolean",
        "colour": "enum",
        "mode": "enum",
        "keywords": "list",
        "nickname": "string",
        "cookies": "secret",
        "extra": "json",
    }
    assert fields["title"].required and not fields["limit"].required
    assert fields["title"].description == "Any text"
    assert (fields["limit"].minimum, fields["limit"].maximum, fields["limit"].value) == (1, 50, 5)
    assert fields["colour"].options == ["red", "blue"]
    assert fields["mode"].options == ["fast", "slow"]
    assert fields["limit"].label == "Limit"


class FakeUpload:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def read(self) -> bytes:
        return self.data


async def test_parse_form_values() -> None:
    parsed = await parse_config_form(
        EveryFieldConfig,
        {
            "cfg.title": " hello ",
            "cfg.limit": "7",
            "cfg.ratio": "",
            "cfg.verbose": "on",
            "cfg.colour": "blue",
            "cfg.keywords": "one\n\n two \nthree",
            "cfg.cookies": FakeUpload(b"cookie=1"),
            "cfg.extra": '{"a": 1}',
        },
    )
    assert parsed.config == {
        "title": "hello",
        "limit": "7",
        "verbose": True,
        "colour": "blue",
        "keywords": ["one", "two", "three"],
        "extra": {"a": 1},
    }
    assert parsed.secrets == {"cookies": b"cookie=1"}
    config = EveryFieldConfig.model_validate(parsed.config)
    assert (config.limit, config.ratio, config.mode) == (7, 0.5, "fast")

    removed = await parse_config_form(
        EveryFieldConfig,
        {"cfg.title": "x", "cfg.cookies": FakeUpload(b""), "cfg.cookies.remove": "on"},
    )
    assert removed.secrets == {"cookies": None}
    assert removed.config["verbose"] is False
    with pytest.raises(FormError) as exc:
        await parse_config_form(EveryFieldConfig, {"cfg.extra": "{oops"})
    assert "extra" in exc.value.fields


async def test_new_source_form_renders_every_field_type(client) -> None:
    page = await client.get("/sources/new?addon=every_field")
    assert page.status_code == 200
    html = page.text
    assert '<input type="text" name="cfg.title" value="" required>' in html
    assert re.search(r'<input type="number" name="cfg.limit" value="5" min="1" max="50"', html)
    assert re.search(r'<input type="number" name="cfg.ratio" value="0.5" step="any"', html)
    assert '<input type="checkbox" name="cfg.verbose">' in html
    assert '<select name="cfg.colour"' in html and '<option value="blue">blue</option>' in html
    assert '<option value="slow">slow</option>' in html
    assert '<textarea name="cfg.keywords" rows="3"></textarea>' in html
    assert '<input type="file" name="cfg.cookies">' in html
    assert '<textarea name="cfg.extra" rows="3"></textarea>' in html
    assert 'enctype="multipart/form-data"' in html
    assert 'name="default_folder_id"' in html and ">Inbox</option>" in html
    assert (await client.get("/sources/new?addon=nope")).status_code == 404


async def test_create_and_edit_through_the_form(client, sessionmaker, registry, paths, db_conn):
    reply = await client.post(
        "/sources/new?addon=every_field",
        data={
            "name": "  Every  thing ",
            "schedule": "*/15 * * * *",
            "enabled": "on",
            "default_tags": "Alpha, beta gamma",
            "max_runtime_minutes": "30",
            "cfg.title": "T",
            "cfg.keywords": "a\nb",
            "cfg.colour": "blue",
        },
        files={"cfg.cookies": ("cookies.txt", b"session=abc\n", "text/plain")},
    )
    assert reply.status_code == 303
    row = db_conn.execute(
        "SELECT id, name, schedule, enabled, default_tags, max_runtime_minutes, config"
        " FROM crawl_sources"
    ).fetchone()
    source_id = row[0]
    assert row[1:6] == ("Every thing", "*/15 * * * *", True, ["alpha", "beta_gamma"], 30)
    assert row[6] == {
        "title": "T",
        "limit": 5,
        "ratio": 0.5,
        "verbose": False,
        "colour": "blue",
        "mode": "fast",
        "keywords": ["a", "b"],
        "nickname": None,
        "extra": {},
    }
    secret = paths.secrets_dir(source_id) / "cookies"
    assert secret.read_bytes() == b"session=abc\n"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert stat.S_IMODE(secret.parent.stat().st_mode) == 0o700

    # The add-on receives the secret as a path.
    async with sessionmaker() as session:
        source = await service.get_source(session, source_id)
    config = service.run_config(registry.get("every_field"), source, paths)
    assert config.cookies == secret

    edit = await client.get(f"/sources/{source_id}/edit")
    assert "A file is stored." in edit.text
    assert 'name="cfg.cookies.remove"' in edit.text
    assert '<option value="blue" selected>blue</option>' in edit.text

    # Invalid values come back with messages and change nothing.
    bad = await client.post(
        f"/sources/{source_id}/edit",
        data={"name": "x", "schedule": "nonsense", "cfg.title": "T", "cfg.limit": "500"},
    )
    assert bad.status_code == 400
    assert "invalid" in bad.text
    assert db_conn.execute("SELECT schedule FROM crawl_sources").fetchone()[0] == "*/15 * * * *"
    bad = await client.post(
        f"/sources/{source_id}/edit", data={"name": "x", "cfg.title": "T", "cfg.limit": "500"}
    )
    assert bad.status_code == 400
    assert "less than or equal to 50" in bad.text

    # Saving without a new upload keeps the secret; ticking remove deletes it.
    ok = await client.post(f"/sources/{source_id}/edit", data={"name": "x", "cfg.title": "U"})
    assert ok.status_code == 303
    assert secret.exists()
    assert db_conn.execute("SELECT enabled FROM crawl_sources").fetchone()[0] is False
    ok = await client.post(
        f"/sources/{source_id}/edit",
        data={"name": "x", "cfg.title": "U", "cfg.cookies.remove": "on"},
    )
    assert ok.status_code == 303
    assert not secret.exists()
