from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.addons import gallerydl
from app.addons.gallerydl import GallerydlConfig, build_args, remote_id_for, tags_from_sidecar
from app.crawl.watch import StableFiles
from tests.crawl.helpers import crawl, crawled_files, fake_tool, outcomes, run_row

CONTENT_SIZE = len(b"FAKE IMAGE site p1 1\n") * 200


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch) -> None:
    monkeypatch.setattr(gallerydl, "POLL_SECONDS", 0.1)
    monkeypatch.setattr(gallerydl, "STABLE_SECONDS", 1.0)


@pytest.fixture
def exe(tmp_path: Path) -> str:
    return fake_tool(tmp_path / "bin", "fake_gallerydl")


def config(exe: str, *urls: str, **extra) -> dict:
    return {"urls": list(urls), "executable": exe, **extra}


def test_tags_from_lists_strings_and_nested_fields() -> None:
    data = {
        "tags": ["Blue Sky", "red", 7, None],
        "tag_string": "one two  three",
        "artist": "Ann",
        "author": {"name": "Bob Lee", "id": 3},
        "count": 5,
        "flag": True,
    }
    assert tags_from_sidecar(
        data,
        ["tags", "tag_string", "artist=artist", "author.name=author", "count", "flag", "missing"],
    ) == [
        "Blue Sky",
        "red",
        "7",
        "one",
        "two",
        "three",
        "artist:Ann",
        "author:Bob",
        "author:Lee",
        "5",
    ]
    assert tags_from_sidecar({"artist": ["Ann", "Cy Twombly"]}, ["artist=artist"]) == [
        "artist:Ann",
        "artist:Cy Twombly",
    ]
    assert tags_from_sidecar({"author": "flat"}, ["author.name"]) == []


def test_remote_id_and_fallback() -> None:
    media = Path("/w/files/x.png")
    assert remote_id_for({"category": "danbooru", "id": 42, "num": 1}, media) == "danbooru:42:1"
    assert remote_id_for({"category": "danbooru", "id": 42}, media) == "danbooru:42"
    assert remote_id_for({"category": "directlink", "filename": "red"}, media) == "directlink:red"
    assert remote_id_for({"category": "directlink", "id": ""}, media) == "directlink:x"


def test_build_args(tmp_path: Path) -> None:
    ctx = SimpleNamespace(work_dir=tmp_path / "w", state_dir=tmp_path / "s")
    cfg = GallerydlConfig(
        urls=["https://example.org/g"],
        range="1-5",
        cookies=tmp_path / "c.txt",
        extra_args=["--sleep", "1"],
    )
    assert build_args("gallery-dl", ctx, cfg) == [
        "gallery-dl",
        "--config-ignore",
        "-D",
        str(tmp_path / "w" / "files"),
        "--write-metadata",
        "--download-archive",
        str(tmp_path / "s" / "gallery-dl-archive.sqlite3"),
        "--cookies",
        str(tmp_path / "c.txt"),
        "--range",
        "1-5",
        "--sleep",
        "1",
        "--",
        "https://example.org/g",
    ]


@pytest.mark.parametrize(
    "bad",
    [
        {"urls": ["-x"]},
        {"urls": ["https://x"], "tag_fields": ["tags=Bad NS"]},
        {"urls": ["https://x"], "tag_fields": ["a..b"]},
        {"urls": ["https://x"], "range": "--exec"},
        {"urls": ["https://x"], "extra_args": ["-o", "base-directory=/"]},
    ],
)
def test_config_rejects(bad: dict) -> None:
    with pytest.raises(ValidationError):
        GallerydlConfig.model_validate(bad)


def test_stable_files_waits_for_sidecar_and_size(tmp_path: Path) -> None:
    now = [100.0]
    files = StableFiles(
        tmp_path, sidecar_for=gallerydl.sidecar_for, stable_seconds=2, clock=lambda: now[0]
    )
    media = tmp_path / "a.png"
    media.write_bytes(b"12")
    (tmp_path / "b.png.part").write_bytes(b"x")
    (tmp_path / "b.png.part.json").write_text("{}")
    assert files.ready() == []  # no sidecar yet
    (tmp_path / "a.png.json").write_text("{}")
    assert files.ready() == []  # first time seen with a sidecar
    now[0] += 1.5
    media.write_bytes(b"1234")
    assert files.ready() == []  # grew: the clock restarts
    now[0] += 1.9
    assert files.ready() == []
    now[0] += 0.2
    assert files.ready() == [media]
    assert files.ready(final=True) == []  # reported once
    assert files.leftovers() == []


async def test_ingests_images_with_tags_from_tag_fields(
    crawl_app, registry, sessionmaker, paths, db_conn, exe
) -> None:
    url = "fake://site/p1?files=2&tags=Blue%20Sky,red&tagstr=one%20two&artist=Ann&author=Bob%20Lee"
    run_id = await crawl(
        sessionmaker,
        registry,
        paths,
        "gallerydl",
        config(exe, url, tag_fields=["tags", "tag_string", "artist=artist", "author.name=by"]),
    )
    assert run_row(db_conn, run_id)[:2] == ("succeeded", None)
    assert outcomes(db_conn, run_id) == {"new": 2}
    files = crawled_files(db_conn, run_id)
    assert set(files) == {"site:p1:1", "site:p1:2"}
    one = files["site:p1:1"]
    assert one["tags"] == ["artist:ann", "blue_sky", "by:bob", "by:lee", "one", "red", "two"]
    assert one["title"] == "Post p1"
    assert one["metadata"]["category"] == "site"
    assert one["metadata"]["subcategory"] == "post"
    assert one["metadata"]["gallerydl"]["filename"] == "site_p1_1"
    assert one["size"] == CONTENT_SIZE


async def test_default_tag_fields(crawl_app, registry, sessionmaker, paths, db_conn, exe) -> None:
    run_id = await crawl(
        sessionmaker,
        registry,
        paths,
        "gallerydl",
        config(exe, "fake://site/d1?tags=a,b&artist=Zed"),
    )
    assert crawled_files(db_conn, run_id)["site:d1:1"]["tags"] == ["a", "artist:zed", "b"]


async def test_growing_file_is_emitted_once_stable(
    crawl_app, registry, sessionmaker, paths, db_conn, exe
) -> None:
    run_id = await crawl(
        sessionmaker, registry, paths, "gallerydl", config(exe, "fake://site/p1?grow=1")
    )
    assert run_row(db_conn, run_id)[:2] == ("succeeded", None)
    assert "taken before it was complete" not in paths.job_log(run_id).read_text()
    assert crawled_files(db_conn, run_id)["site:p1:1"]["size"] == CONTENT_SIZE


async def test_partial_files_are_ignored_and_fallback_remote_id(
    crawl_app, registry, sessionmaker, paths, db_conn, exe
) -> None:
    run_id = await crawl(
        sessionmaker,
        registry,
        paths,
        "gallerydl",
        config(exe, "fake://site/p2?partial=1", "fake://site/p3?noid=1"),
    )
    assert run_row(db_conn, run_id)[:2] == ("succeeded", None)
    assert set(crawled_files(db_conn, run_id)) == {"site:site_p3_1"}


async def test_files_are_emitted_while_the_process_runs(
    crawl_app, registry, sessionmaker, paths, db_conn, exe
) -> None:
    run_id = await crawl(
        sessionmaker, registry, paths, "gallerydl", config(exe, "fake://site/s?files=3&slow=1")
    )
    assert run_row(db_conn, run_id)[:2] == ("succeeded", None)
    log = paths.job_log(run_id).read_text()
    for n in (1, 2, 3):
        assert f"[fake] site_s_{n} was taken while running" in log
    assert outcomes(db_conn, run_id) == {"new": 3}


async def test_missing_sidecar_and_failing_exit(
    crawl_app, registry, sessionmaker, paths, db_conn, exe
) -> None:
    run_id = await crawl(
        sessionmaker,
        registry,
        paths,
        "gallerydl",
        config(exe, "fake://site/ok", "fake://site/lone?nosidecar=1&fail=1"),
    )
    status, error, _, emitted = run_row(db_conn, run_id)
    assert (status, error, emitted) == ("succeeded", None, 1)
    log = paths.job_log(run_id).read_text()
    assert "no metadata sidecar for site_lone_1.png; not emitted" in log
    assert "gallery-dl exited with 4 after 1 file(s) were emitted" in log

    failed = await crawl(
        sessionmaker, registry, paths, "gallerydl", config(exe, "fake://site/?files=0&fail=1")
    )
    status, error, _, emitted = run_row(db_conn, failed)
    assert (status, emitted) == ("failed", 0)
    assert error == "RuntimeError: gallery-dl exited with 4: [site][error] HttpError: 404 Not Found"


async def test_recrawl_skips_known_remote_ids(
    crawl_app, registry, sessionmaker, paths, db_conn, exe
) -> None:
    first = await crawl(sessionmaker, registry, paths, "gallerydl", config(exe, "fake://site/r1"))
    assert outcomes(db_conn, first) == {"new": 1}
    # Another source (own gallery-dl archive): the database still knows the remote id.
    second = await crawl(sessionmaker, registry, paths, "gallerydl", config(exe, "fake://site/r1"))
    assert outcomes(db_conn, second) == {"skipped_known_remote": 1}
