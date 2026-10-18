"""Phase 8 smoke check: authentication against a real uvicorn started with APP_PASSWORD.

Runs where the app runs (web container, or locally with SMOKE_LOCAL=1): a second web server on
127.0.0.1:<port> with the same database and DATA_DIR, so the stack under test keeps running
without a password. Prints PASS lines; exits non-zero on the first failure.
"""

import os
import subprocess
import sys
import time

import httpx

PORT = os.environ.get("SMOKE_AUTH_PORT", "8765")
BASE = f"http://127.0.0.1:{PORT}"
PASSWORD = "smoke-password"  # noqa: S105 - throwaway password of the check server
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


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    env = {**os.environ, "APP_PASSWORD": PASSWORD, "SECRET_KEY": "smoke-" + "k" * 40}
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", PORT],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with httpx.Client(base_url=BASE, timeout=10) as http:
            for _ in range(60):
                try:
                    if http.get("/healthz").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.5)
            else:
                fail("the password-protected server did not start")
            check(http)
    finally:
        server.terminate()
        try:
            _, err = server.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            _, err = server.communicate()
    if "APP_PASSWORD is not set" in (err or ""):
        fail("the protected server logged the no-password warning")


def check(http: httpx.Client) -> None:
    for path in PAGES:
        r = http.get(path)
        if r.status_code != 303 or not r.headers.get("location", "").startswith("/login?next="):
            fail(f"GET {path} without a session returned {r.status_code}")
    print(f"PASS with APP_PASSWORD every page redirects to /login: {', '.join(PAGES)}")
    if http.get("/api/library").status_code != 401:
        fail("API without a session is not 401")
    if http.get("/healthz").status_code != 200 or http.get("/static/app.css").status_code != 200:
        fail("public routes are not public")
    if http.get("/login").status_code != 200:
        fail("GET /login")
    print("PASS /healthz, /login and /static stay public; the API answers 401")

    origin = {"Origin": BASE}
    if http.post("/login", data={"password": PASSWORD}).status_code != 403:
        fail("POST /login without Origin was not refused")
    wrong = http.post("/login", data={"password": "wrong"}, headers=origin)
    if wrong.status_code != 401:
        fail(f"wrong password returned {wrong.status_code}")
    started = time.monotonic()
    wrong = http.post("/login", data={"password": "wrong"}, headers=origin)
    waited = time.monotonic() - started
    if wrong.status_code != 401 or waited < 0.9:
        fail(f"second failed login was not slowed down ({waited:.2f} s)")
    print(f"PASS failed logins are refused and slowed down (second attempt took {waited:.1f} s)")

    started = time.monotonic()
    ok = http.post("/login", data={"password": PASSWORD, "next": "/settings"}, headers=origin)
    if ok.status_code != 303 or ok.headers.get("location") != "/settings":
        fail(f"login returned {ok.status_code} {ok.headers.get('location')}")
    cookie = ok.headers.get("set-cookie", "")
    flags = {p.strip().lower() for p in cookie.split(";")}
    if not {"httponly", "samesite=lax", "max-age=2592000"} <= flags:
        fail(f"cookie flags: {cookie}")
    for path in PAGES:
        if http.get(path).status_code != 200:
            fail(f"GET {path} with a session")
    print("PASS login sets an HttpOnly, SameSite=Lax, 30-day cookie; every page then loads")

    if http.post("/api/debug/ping").status_code != 403:
        fail("POST with a session but without Origin was not refused")
    other = http.post("/api/debug/ping", headers={"Origin": "http://evil.example"})
    if other.status_code != 403:
        fail("cross-origin POST was not refused")
    if http.post("/api/debug/ping", headers=origin).status_code != 202:
        fail("same-origin POST was refused")
    print("PASS unsafe methods need a same-origin Origin or Referer (403 otherwise)")
    if http.post("/logout", headers=origin).status_code != 303 or http.get("/").status_code != 303:
        fail("logout")
    print("PASS logout ends the session")


if __name__ == "__main__":
    main()
