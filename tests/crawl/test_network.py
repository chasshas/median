"""Real-network smoke tests for the built-in add-ons (skipped unless run with -m network).

They use the real tools from the tool venv (MEDIAN_TOOLS_BIN, default /data/tools/venv/bin,
where the worker installs them) and URLs you are allowed to download:

    MEDIAN_NET_YTDLP_URL=<one short video>
    MEDIAN_NET_GALLERYDL_URL=<one small gallery>
    MEDIAN_NET_DIRECT_URLS="<image url> <image url>"
    make test ARGS="-m network tests/crawl/test_network.py"
"""

import os
import re
from pathlib import Path

import pytest

from tests.crawl.helpers import crawl, crawled_files, outcomes, recrawl, run_row

pytestmark = pytest.mark.network

TOOLS_BIN = Path(os.environ.get("MEDIAN_TOOLS_BIN", "/data/tools/venv/bin"))


def need(env: str) -> str:
    value = os.environ.get(env, "").strip()
    if not value:
        pytest.skip(f"set {env}")
    return value


def tool(name: str) -> str:
    path = TOOLS_BIN / name
    if not path.exists():
        pytest.skip(f"{path} not installed (run python -m app.cli tools or start the worker)")
    return str(path)


async def test_ytdlp_real(crawl_app, registry, sessionmaker, paths, db_conn) -> None:
    url = need("MEDIAN_NET_YTDLP_URL")
    cfg = {"urls": [url], "executable": tool("yt-dlp"), "extra_args": ["--verbose"]}
    run_id = await crawl(sessionmaker, registry, paths, "ytdlp", cfg)
    status, error, source_id, _ = run_row(db_conn, run_id)
    log = paths.job_log(run_id).read_text()
    assert (status, error) == ("succeeded", None), log[-3000:]
    assert outcomes(db_conn, run_id) == {"new": 1}
    (item,) = crawled_files(db_conn, run_id).values()
    assert any(t.startswith("uploader:") for t in item["tags"]), item["tags"]
    runtimes = re.search(r"\[debug\] JS runtimes: (.*)", log)
    assert runtimes and runtimes.group(1) != "none", runtimes

    again = await recrawl(sessionmaker, source_id)
    assert run_row(db_conn, again)[:2] == ("succeeded", None)
    assert outcomes(db_conn, again) == {}
    assert "has already been recorded in the archive" in paths.job_log(again).read_text()


async def test_gallerydl_real(crawl_app, registry, sessionmaker, paths, db_conn) -> None:
    url = need("MEDIAN_NET_GALLERYDL_URL")
    cfg = {"urls": [url], "executable": tool("gallery-dl")}
    run_id = await crawl(sessionmaker, registry, paths, "gallerydl", cfg)
    status, error, _, emitted = run_row(db_conn, run_id)
    assert (status, error) == ("succeeded", None), paths.job_log(run_id).read_text()[-3000:]
    assert emitted >= 1
    assert outcomes(db_conn, run_id).get("new", 0) >= 1
    files = crawled_files(db_conn, run_id)
    assert all(rid and ":" in rid for rid in files)
    print("tags:", {rid: f["tags"] for rid, f in files.items()})


async def test_direct_real(crawl_app, registry, sessionmaker, paths, db_conn) -> None:
    urls = need("MEDIAN_NET_DIRECT_URLS").split()
    run_id = await crawl(sessionmaker, registry, paths, "direct", {"urls": urls})
    assert run_row(db_conn, run_id)[:2] == ("succeeded", None)
    assert outcomes(db_conn, run_id) == {"new": len(urls)}
