from pathlib import Path

import httpx

from app.addons.demo import DemoAddon
from app.crawl.registry import ENTRY_POINT_GROUP, Registry, discover
from tests.conftest import SAME_ORIGIN

GOOD = """
from pydantic import BaseModel
from app.crawl.base import Addon


class Config(BaseModel):
    url: str = "https://example.org"


class Local(Addon):
    key = "local_one"
    name = "Local one"
    version = "2.0"
    ConfigModel = Config

    async def run(self, ctx, config):
        ctx.log.info("local")


ADDON = Local
"""

DUPLICATE = """
from app.crawl.base import Addon


class Fake(Addon):
    key = "demo"

    async def run(self, ctx, config):
        pass


ADDON = Fake()
"""


def write_addons(directory: Path, files: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / name).write_text(text)
    return directory


def test_entry_point_demo_is_discovered() -> None:
    import importlib.metadata

    names = [ep.name for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)]
    assert {"demo", "direct", "gallerydl", "ytdlp"} <= set(names)
    registry = discover(None)
    info = registry.get("demo")
    assert info is not None
    assert isinstance(info.addon, DemoAddon)
    assert info.origin == "entry point demo = app.addons.demo:DemoAddon"
    assert registry.errors == []


def test_single_file_addon(tmp_path: Path) -> None:
    registry = discover(write_addons(tmp_path / "addons", {"local.py": GOOD}))

    info = registry.get("local_one")
    assert info is not None
    assert (info.name, info.version) == ("Local one", "2.0")
    assert info.origin == str(tmp_path / "addons" / "local.py")
    assert info.config_model.model_validate({}).url == "https://example.org"
    assert "demo" in registry.addons
    assert registry.errors == []


def test_broken_addons_are_reported_and_skipped(tmp_path: Path) -> None:
    addons = write_addons(
        tmp_path / "addons",
        {
            "a_syntax.py": "def broken(:\n",
            "b_raises.py": "raise RuntimeError('cannot import this')\n",
            "c_no_addon.py": "x = 1\n",
            "d_not_addon.py": "ADDON = object()\n",
            "e_bad_key.py": GOOD.replace('"local_one"', '"Bad Key"'),
            "f_exit.py": "import sys\nsys.exit(3)\n",
            "g_no_run.py": GOOD.replace("async def run", "def run"),
            "good.py": GOOD,
            "_ignored.py": "raise SystemExit",
        },
    )
    registry = discover(addons)

    assert set(registry.addons) == {"demo", "direct", "gallerydl", "ytdlp", "local_one"}
    errors = {Path(e.origin).name: e.error for e in registry.errors}
    assert set(errors) == {
        "a_syntax.py",
        "b_raises.py",
        "c_no_addon.py",
        "d_not_addon.py",
        "e_bad_key.py",
        "f_exit.py",
        "g_no_run.py",
    }
    assert "SyntaxError" in errors["a_syntax.py"]
    assert "cannot import this" in errors["b_raises.py"]
    assert "no module-level ADDON" in errors["c_no_addon.py"]
    assert "invalid add-on key" in errors["e_bad_key.py"]
    assert "SystemExit" in errors["f_exit.py"]
    assert "async" in errors["g_no_run.py"]


def test_duplicate_keys_are_errors(tmp_path: Path) -> None:
    addons = write_addons(
        tmp_path / "addons",
        {"dup_demo.py": DUPLICATE, "one.py": GOOD, "two.py": GOOD.replace("Local one", "Two")},
    )
    registry = discover(addons)

    assert isinstance(registry.addons["demo"].addon, DemoAddon)  # the entry point wins
    assert registry.addons["local_one"].name == "Local one"  # the first file wins
    dup = {Path(e.origin).name: e for e in registry.errors}
    assert set(dup) == {"dup_demo.py", "two.py"}
    assert dup["dup_demo.py"].key == "demo"
    assert "duplicate add-on key 'demo'" in dup["dup_demo.py"].error
    assert "entry point demo" in dup["dup_demo.py"].error


async def test_sources_page_lists_addon_errors(app, tmp_path: Path, clean_ingest) -> None:
    app.state.registry = discover(
        write_addons(
            tmp_path / "addons", {"bad.py": "raise ValueError('boom')\n", "dup.py": DUPLICATE}
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=SAME_ORIGIN
    ) as client:
        page = await client.get("/sources")
        api = await client.get("/api/sources")

    assert page.status_code == 200
    assert 'id="addon-errors"' in page.text
    assert "ValueError: boom" in page.text
    assert "duplicate add-on key" in page.text
    assert '<option value="demo">' in page.text
    errors = api.json()["addon_errors"]
    assert {Path(e["origin"]).name for e in errors} == {"bad.py", "dup.py"}


def test_registry_add_rejects_non_addons() -> None:
    registry = Registry()
    registry.add(int, "int")
    registry.add(DemoAddon, "demo")
    registry.add(DemoAddon(), "demo again")
    assert list(registry.addons) == ["demo"]
    assert [e.origin for e in registry.errors] == ["int", "demo again"]
