import threading
from collections import Counter
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import ValidationError

from app.addons.direct import DirectConfig, filename_from_disposition, normalize_url
from tests.crawl.helpers import crawl, crawled_files, outcomes, recrawl, run_row
from tests.media import png_bytes

MEGABYTE = 1024 * 1024


class Server:
    def __init__(self) -> None:
        self.hits: Counter[str] = Counter()
        self.routes: dict[str, tuple[dict[str, str], bytes, bool]] = {}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = self.path.split("?")[0]
                server.hits[path] += 1
                if path not in server.routes:
                    self.send_error(404)
                    return
                headers, body, chunked = server.routes[path]
                self.send_response(200)
                for key, value in headers.items():
                    self.send_header(key, value)
                if chunked:
                    self.send_header("Connection", "close")
                else:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def route(self, path: str, body: bytes, chunked: bool = False, **headers: str) -> str:
        self.routes[path] = ({k.replace("_", "-"): v for k, v in headers.items()}, body, chunked)
        return self.base + path


@pytest.fixture
def server() -> Iterator[Server]:
    srv = Server()
    thread = threading.Thread(target=srv.httpd.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.httpd.shutdown()
    srv.httpd.server_close()


def test_normalize_url() -> None:
    assert (
        normalize_url("HTTPS://User:Pw@Example.ORG:8443/A/B.png?Q=1#frag")
        == "https://User:Pw@example.org:8443/A/B.png?Q=1"
    )
    assert normalize_url("http://example.org") == "http://example.org"


def test_content_disposition() -> None:
    assert filename_from_disposition('attachment; filename="a b.png"') == "a b.png"
    assert (
        filename_from_disposition("attachment; filename=x.png; filename*=UTF-8''%ED%95%9C.png")
        == "한.png"
    )
    assert filename_from_disposition("inline") is None
    assert filename_from_disposition(None) is None


def test_config_rejects_non_http() -> None:
    for urls in ([], ["ftp://x/a"], ["file:///etc/passwd"], ["-o"]):
        with pytest.raises(ValidationError):
            DirectConfig(urls=urls)


async def test_two_images_with_tags(
    crawl_app, registry, sessionmaker, paths, db_conn, server
) -> None:
    a = server.route("/img/a.png", png_bytes(color=(200, 0, 0)), Content_Type="image/png")
    b = server.route(
        "/download",
        png_bytes(color=(0, 0, 200)),
        Content_Type="image/png",
        Content_Disposition='attachment; filename="Holiday Photo.png"',
    )
    run_id = await crawl(
        sessionmaker,
        registry,
        paths,
        "direct",
        {"urls": [a, b + "#top"], "tags": ["Trip", "artist:Ann"]},
    )
    assert run_row(db_conn, run_id)[:2] == ("succeeded", None)
    assert outcomes(db_conn, run_id) == {"new": 2}
    files = crawled_files(db_conn, run_id)
    assert set(files) == {a, b}
    assert files[b]["url"] == b + "#top"
    assert files[a]["tags"] == ["artist:ann", "trip"]
    names = dict(
        db_conn.execute(
            "SELECT s.remote_id, f.original_filename FROM file_sources s"
            " JOIN files f ON f.id = s.file_id WHERE s.job_run_id = %s",
            (run_id,),
        ).fetchall()
    )
    assert names == {a: "a.png", b: "Holiday Photo.png"}


async def test_known_url_is_skipped_without_a_request(
    crawl_app, registry, sessionmaker, paths, db_conn, server
) -> None:
    url = server.route("/k.png", png_bytes(color=(0, 120, 0)))
    first = await crawl(sessionmaker, registry, paths, "direct", {"urls": [url]})
    assert server.hits["/k.png"] == 1
    source_id = run_row(db_conn, first)[2]
    second = await recrawl(sessionmaker, source_id)
    assert run_row(db_conn, second)[:2] == ("succeeded", None)
    assert server.hits["/k.png"] == 1
    assert "known, skipped" in paths.job_log(second).read_text()
    # Same URL written differently (host case, fragment): still known.
    other = url.replace("127.0.0.1", "127.0.0.1").replace("http://", "HTTP://") + "#x"
    await crawl(sessionmaker, registry, paths, "direct", {"urls": [other]})
    assert server.hits["/k.png"] == 1


async def test_size_limit(crawl_app, registry, sessionmaker, paths, db_conn, server) -> None:
    big = b"\0" * (MEGABYTE + 10)
    declared = server.route("/big.bin", big)
    streamed = server.route("/stream.bin", big, chunked=True)
    small = server.route("/small.png", png_bytes(color=(9, 9, 9)))
    run_id = await crawl(
        sessionmaker,
        registry,
        paths,
        "direct",
        {"urls": [declared, streamed, small], "max_size_mb": 1},
    )
    status, error, _, emitted = run_row(db_conn, run_id)
    assert (status, error, emitted) == ("succeeded", None, 1)
    assert set(crawled_files(db_conn, run_id)) == {small}
    log = paths.job_log(run_id).read_text()
    assert f"skipped {declared}: {MEGABYTE + 10} bytes is over the limit" in log
    assert f"skipped {streamed}: more than the limit of {MEGABYTE} bytes" in log

    failed = await crawl(
        sessionmaker, registry, paths, "direct", {"urls": [server.base + "/missing"]}
    )
    status, error, _, _ = run_row(db_conn, failed)
    assert (status, error) == ("failed", "RuntimeError: all 1 download(s) failed")
