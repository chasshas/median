import logging
import time

import httpx
import pytest

from app.main import create_app
from app.ops.auth import (
    COOKIE_NAME,
    LoginLimiter,
    make_token,
    safe_next,
    same_origin,
    token_valid,
)

PASSWORD = "correct horse"


@pytest.fixture
async def secured(settings):
    app = create_app(settings.model_copy(update={"app_password": PASSWORD, "secret_key": "k" * 40}))
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    app.state.login_limiter = LoginLimiter(sleep=fake_sleep)
    app.state.slept = slept
    async with app.router.lifespan_context(app):
        yield app


def client_for(app, **headers) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Origin": "http://test", **headers},
    )


async def login(http: httpx.AsyncClient, password: str = PASSWORD, next: str = "/"):
    return await http.post("/login", data={"password": password, "next": next})


PAGES = [
    "/",
    "/upload",
    "/review",
    "/library",
    "/search",
    "/tags",
    "/sources",
    "/jobs",
    "/settings",
]


async def test_every_page_redirects_to_login(secured) -> None:
    async with client_for(secured) as http:
        for path in [*PAGES, "/files/1", "/media/1/thumb"]:
            r = await http.get(path, params={"x": "1"} if path == "/search" else None)
            assert r.status_code == 303, path
            assert r.headers["location"].startswith("/login?next=%2F"), path
        assert (await http.get("/search?q=cat")).headers[
            "location"
        ] == "/login?next=%2Fsearch%3Fq%3Dcat"
        api = await http.get("/api/library")
        assert api.status_code == 401 and api.json() == {"detail": "login required"}
        post = await http.post("/api/review/archive", json={"file_ids": [1]})
        assert post.status_code == 401
        htmx = await http.get("/review/fragments/grid?ids=1", headers={"HX-Request": "true"})
        assert htmx.status_code == 401 and htmx.headers["HX-Redirect"].startswith("/login")


async def test_public_routes(secured) -> None:
    async with client_for(secured) as http:
        assert (await http.get("/healthz")).status_code == 200
        page = await http.get("/login")
        assert page.status_code == 200 and 'name="password"' in page.text
        assert (await http.get("/static/app.css")).status_code == 200


async def test_login_sets_a_signed_session_cookie(secured) -> None:
    async with client_for(secured) as http:
        r = await login(http, next="/settings")
        assert r.status_code == 303 and r.headers["location"] == "/settings"
        cookie = r.headers["set-cookie"]
        assert cookie.startswith(f"{COOKIE_NAME}=")
        flags = {part.strip().lower() for part in cookie.split(";")}
        assert "httponly" in flags
        assert "samesite=lax" in flags
        assert "max-age=2592000" in flags  # 30 days
        assert "path=/" in flags
        assert "secure" not in flags  # plain http here
        for path in PAGES:
            assert (await http.get(path)).status_code == 200, path
        assert "No password set" not in (await http.get("/")).text
        assert 'id="logout"' in (await http.get("/")).text
        # Signed in: /login goes on to the target.
        assert (await http.get("/login?next=/jobs")).headers["location"] == "/jobs"

        out = await http.post("/logout")
        assert out.status_code == 303 and out.headers["location"] == "/login"
        assert (await http.get("/")).status_code == 303


async def test_tampered_expired_and_old_password_cookies_are_refused(secured) -> None:
    key = secured.state.session_key.value
    good = make_token(key, PASSWORD)
    issued, _, sig = good.partition(".")
    for value in [
        f"{int(issued) + 10}.{sig}",
        good[:-2] + "xx",
        make_token(key, PASSWORD, now=time.time() - 31 * 24 * 3600),
        make_token(key, "the old password"),
        make_token(b"another key", PASSWORD),
        "garbage",
    ]:
        async with client_for(secured, Cookie=f"{COOKIE_NAME}={value}") as http:
            assert (await http.get("/")).status_code == 303, value
    async with client_for(secured, Cookie=f"{COOKIE_NAME}={good}") as http:
        assert (await http.get("/")).status_code == 200
    assert token_valid(key, PASSWORD, good)


async def test_failed_logins_slow_down_per_ip(secured) -> None:
    slept = secured.state.slept
    async with client_for(secured) as http:
        for _ in range(4):
            r = await login(http, "wrong")
            assert r.status_code == 401 and "Wrong password." in r.text
        assert slept == [1.0, 2.0, 4.0]  # nothing before the first attempt, then doubling
        ok = await login(http)
        assert ok.status_code == 303
        assert slept == [1.0, 2.0, 4.0, 8.0]  # the correct password still waits its turn
        await login(http, "wrong")
        assert slept[-1] == 8.0 and len(slept) == 4  # a success reset the count


def test_limiter_caps_forgets_and_separates_clients() -> None:
    now = [0.0]
    limiter = LoginLimiter(clock=lambda: now[0])
    for _ in range(10):
        limiter.failed("10.0.0.1")
    assert limiter.delay("10.0.0.1") == LoginLimiter.MAX
    assert limiter.delay("10.0.0.2") == 0
    now[0] += LoginLimiter.FORGET + 1
    assert limiter.delay("10.0.0.1") == 0


async def test_unsafe_methods_need_same_origin(client) -> None:
    async with httpx.AsyncClient(transport=client._transport, base_url="http://test") as bare:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            r = await bare.request(method, "/api/tags/1")
            assert r.status_code == 403, method
        assert (
            await bare.post("/api/debug/ping", headers={"Origin": "http://evil.example"})
        ).status_code == 403
        assert (await bare.post("/api/debug/ping", headers={"Origin": "null"})).status_code == 403
        assert (
            await bare.post("/api/debug/ping", headers={"Referer": "http://evil.example/x"})
        ).status_code == 403
        ok = await bare.post("/api/debug/ping", headers={"Referer": "http://test/upload"})
        assert ok.status_code == 202
        assert (await bare.get("/api/library")).status_code == 200  # safe methods need nothing
    assert (await client.post("/api/debug/ping")).status_code == 202  # Origin: http://test


async def test_login_post_needs_same_origin_too(secured) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=secured), base_url="http://test"
    ) as http:
        r = await http.post("/login", data={"password": PASSWORD})
        assert r.status_code == 403


def test_same_origin_and_safe_next() -> None:
    assert same_origin({"host": "media.lan:8000", "origin": "https://media.lan:8000"})
    assert not same_origin({"host": "media.lan:8000", "origin": "http://media.lan:9000"})
    assert not same_origin({"host": "media.lan", "origin": "http://media.lan.evil.com"})
    assert same_origin({"host": "media.lan", "referer": "http://media.lan/review?x=1"})
    assert not same_origin({"host": "media.lan"})
    assert safe_next("/review?x=1") == "/review?x=1"
    for bad in ["https://evil.com", "//evil.com", "/\\evil.com", "", None]:
        assert safe_next(bad) == "/"


async def test_banner_and_warning_without_password(settings, caplog) -> None:
    app = create_app(settings)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        async with app.router.lifespan_context(app), client_for(app) as http:
            page = await http.get("/")
            assert page.status_code == 200
            assert 'id="no-password-banner"' in page.text and "No password set" in page.text
            assert 'id="logout"' not in page.text
            assert (await http.get("/login")).headers["location"] == "/"
    assert any("APP_PASSWORD is not set" in r.getMessage() for r in caplog.records)


async def test_generated_session_key_is_kept_in_data_dir(settings, paths) -> None:
    app = create_app(settings.model_copy(update={"app_password": PASSWORD, "secret_key": ""}))
    async with app.router.lifespan_context(app), client_for(app) as http:
        r = await login(http)
        assert r.status_code == 303
        key_file = paths.secrets / "session_key"
        assert key_file.is_file() and (key_file.stat().st_mode & 0o777) == 0o600
        assert (await http.get("/")).status_code == 200
