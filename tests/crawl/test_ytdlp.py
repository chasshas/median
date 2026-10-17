from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.addons import ytdlp
from app.addons.ytdlp import YtdlpConfig, archive_id, build_args, parse_done_line
from tests.crawl.helpers import crawl, crawled_files, fake_tool, outcomes, recrawl, run_row

URL = "fake://fakesite/a1,a2?uploader=Jane%20Doe&tags=Cats,Outdoor%20Life&categories=Music"


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch) -> None:
    monkeypatch.setattr(ytdlp, "POLL_SECONDS", 0.1)


@pytest.fixture
def exe(tmp_path: Path) -> str:
    return fake_tool(tmp_path / "bin", "fake_ytdlp")


def config(exe: str, *urls: str, **extra) -> dict:
    return {"urls": list(urls), "executable": exe, **extra}


def test_archive_id_matches_yt_dlp() -> None:
    # yt_dlp.utils.make_archive_id: f"{ie_key.lower()} {video_id}"
    assert archive_id("Youtube", "dQw4w9WgXcQ") == "youtube dQw4w9WgXcQ"
    assert parse_done_line("Youtube abc\t/w/Youtube/abc.mp4\t/w/Youtube/abc.info.json") == (
        "youtube abc",
        Path("/w/Youtube/abc.mp4"),
        Path("/w/Youtube/abc.info.json"),
    )
    assert parse_done_line("Generic id with\ttab\t/w/a.mp4\tNA") == (
        "generic id with\ttab",
        Path("/w/a.mp4"),
        None,
    )
    assert parse_done_line("garbage") is None


def test_build_args(tmp_path: Path) -> None:
    ctx = SimpleNamespace(work_dir=tmp_path / "work%1")
    cfg = YtdlpConfig(
        urls=["https://example.org/v"],
        max_items=3,
        date_after="20260101",
        cookies=tmp_path / "cookies.txt",
        extra_args=["--limit-rate", "1M"],
    )
    args = build_args("yt-dlp", ctx, cfg, tmp_path / "a.txt", tmp_path / "work%1" / "done.tsv")
    assert args[:2] == ["yt-dlp", "--ignore-config"]
    assert args[args.index("-o") + 1] == f"{tmp_path}/work%%1/%(extractor_key)s/%(id)s.%(ext)s"
    assert args[args.index("-f") + 1] == "bv*+ba/b"
    assert "--write-info-json" in args and "--no-progress" in args
    assert args[args.index("--download-archive") + 1] == str(tmp_path / "a.txt")
    i = args.index("--print-to-file")
    assert args[i + 1] == "after_move:%(extractor_key)s %(id)s\t%(filepath)s\t%(infojson_filename)s"
    assert args[i + 2] == f"{tmp_path}/work%%1/done.tsv"
    assert args[args.index("--cookies") + 1] == str(tmp_path / "cookies.txt")
    assert args[args.index("--dateafter") + 1] == "20260101"
    assert args[args.index("--max-downloads") + 1] == "3"
    assert args[-4:] == ["--limit-rate", "1M", "--", "https://example.org/v"]


@pytest.mark.parametrize(
    "bad",
    [
        {"urls": []},
        {"urls": ["--exec=id"]},
        {"urls": ["https://x"], "extra_args": ["--exec", "id"]},
        {"urls": ["https://x"], "date_after": "2026-01-01"},
        {"urls": ["https://x"], "max_items": 0},
    ],
)
def test_config_rejects(bad: dict) -> None:
    with pytest.raises(ValidationError):
        YtdlpConfig.model_validate(bad)


async def test_ingests_with_tags_and_archive_from_database(
    crawl_app, registry, sessionmaker, paths, db_conn, exe
) -> None:
    run_id = await crawl(sessionmaker, registry, paths, "ytdlp", config(exe, URL))
    status, error, source_id, emitted = run_row(db_conn, run_id)
    assert (status, error, emitted) == ("succeeded", None, 2)
    assert outcomes(db_conn, run_id) == {"new": 2}
    files = crawled_files(db_conn, run_id)
    assert set(files) == {"fakesite a1", "fakesite a2"}
    a1 = files["fakesite a1"]
    assert a1["url"] == "https://fakesite.example/watch/a1"
    assert (a1["title"], a1["description"]) == ("Video a1", "About a1")
    assert a1["tags"] == ["cats", "music", "outdoor_life", "uploader:jane_doe"]
    assert {k: a1["metadata"][k] for k in ytdlp.METADATA_KEYS} == {
        "id": "a1",
        "uploader": "Jane Doe",
        "upload_date": "20260102",
        "duration": 12,
        "extractor": "fakesite",
    }

    # The state directory is lost: the archive is rebuilt from the database before the run.
    archive = paths.addon_state_dir(source_id) / ytdlp.ARCHIVE_NAME
    archive.unlink()
    run2 = await recrawl(sessionmaker, source_id)
    assert run_row(db_conn, run2)[:2] == ("succeeded", None)
    assert run_row(db_conn, run2)[3] == 0
    assert outcomes(db_conn, run2) == {}
    assert sorted(archive.read_text().splitlines()) == ["fakesite a1", "fakesite a2"]
    log = paths.job_log(run2).read_text()
    assert "a1 has already been recorded in the archive" in log
    assert "a2 has already been recorded in the archive" in log


async def test_known_ids_of_another_source_are_in_the_archive(
    crawl_app, registry, sessionmaker, paths, db_conn, exe
) -> None:
    first = await crawl(sessionmaker, registry, paths, "ytdlp", config(exe, "fake://fakesite/z1"))
    assert run_row(db_conn, first)[3] == 1
    second = await crawl(
        sessionmaker, registry, paths, "ytdlp", config(exe, "fake://fakesite/z1,z2")
    )
    assert set(crawled_files(db_conn, second)) == {"fakesite z2"}


async def test_files_are_emitted_while_the_process_runs(
    crawl_app, registry, sessionmaker, paths, db_conn, exe
) -> None:
    run_id = await crawl(
        sessionmaker, registry, paths, "ytdlp", config(exe, "fake://fakesite/s1,s2,s3?slow=1")
    )
    assert run_row(db_conn, run_id)[:2] == ("succeeded", None)
    log = paths.job_log(run_id).read_text()
    for vid in ("s1", "s2", "s3"):
        assert f"[fake] {vid} was taken while running" in log
    assert outcomes(db_conn, run_id) == {"new": 3}


async def test_nonzero_exit_after_files_is_a_warning(
    crawl_app, registry, sessionmaker, paths, db_conn, exe
) -> None:
    run_id = await crawl(
        sessionmaker,
        registry,
        paths,
        "ytdlp",
        config(exe, "fake://fakesite/ok1,ok2", "fake://fakesite/?fail=1"),
    )
    status, error, _, emitted = run_row(db_conn, run_id)
    assert (status, error, emitted) == ("succeeded", None, 2)
    log = paths.job_log(run_id).read_text()
    assert "WARNING yt-dlp exited with 1 after 2 file(s) were emitted" in log


async def test_nonzero_exit_without_files_fails(
    crawl_app, registry, sessionmaker, paths, db_conn, exe
) -> None:
    run_id = await crawl(
        sessionmaker, registry, paths, "ytdlp", config(exe, "fake://fakesite/?fail=1")
    )
    status, error, _, emitted = run_row(db_conn, run_id)
    assert (status, emitted) == ("failed", 0)
    assert (
        error == "RuntimeError: yt-dlp exited with 1: ERROR: [fakesite] broken: Unable to"
        " download webpage"
    )


async def test_max_items_exit_101_succeeds(
    crawl_app, registry, sessionmaker, paths, db_conn, exe
) -> None:
    run_id = await crawl(
        sessionmaker,
        registry,
        paths,
        "ytdlp",
        config(exe, "fake://fakesite/m1,m2,m3", max_items=2, date_after="20250101"),
    )
    status, error, _, emitted = run_row(db_conn, run_id)
    assert (status, error, emitted) == ("succeeded", None, 2)
    log = paths.job_log(run_id).read_text()
    assert "--max-downloads 2 --dateafter" not in log  # order: dateafter, then max-downloads
    assert "--dateafter 20250101 --max-downloads 2" in log
    assert "exit code 101" in log


async def test_options_and_categories_switched_off(
    crawl_app, registry, sessionmaker, paths, db_conn, exe
) -> None:
    run_id = await crawl(
        sessionmaker,
        registry,
        paths,
        "ytdlp",
        config(exe, URL, include_uploader_tag=False, include_categories=False),
    )
    files = crawled_files(db_conn, run_id)
    assert files["fakesite a1"]["tags"] == ["cats", "outdoor_life"]
