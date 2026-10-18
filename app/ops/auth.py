"""Single-user authentication and the same-origin check.

When APP_PASSWORD is set, every route except /healthz, /login and /static/ needs a session
cookie: "<issued unix time>.<HMAC-SHA256>" signed with SECRET_KEY (or a random key kept in
DATA_DIR/secrets/session_key) over the issue time and a hash of the password, so changing the
password ends every session. Cookies are HttpOnly, SameSite=Lax and last 30 days.

Unsafe methods (POST, PUT, PATCH, DELETE) must carry an Origin or Referer header naming the
host of the request, with or without a password; anything else gets 403.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings
from app.storage import DataPaths
from app.storage.fs import write_private_file

logger = logging.getLogger(__name__)

COOKIE_NAME = "median_session"
SESSION_SECONDS = 30 * 24 * 3600
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PUBLIC_PATHS = frozenset({"/healthz", "/login"})
PUBLIC_PREFIXES = ("/static/",)


# Session tokens ------------------------------------------------------------------------------


def load_session_key(settings: Settings, paths: DataPaths) -> bytes:
    """SECRET_KEY, or a random key created once in DATA_DIR/secrets/session_key (0600)."""
    if settings.secret_key:
        return settings.secret_key.encode()
    path: Path = paths.secrets / "session_key"
    try:
        key = path.read_bytes().strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32).encode()
    write_private_file(path, key + b"\n")
    logger.warning("SECRET_KEY is not set; created a random session key in %s", path)
    return key


class SessionKey:
    """The signing key, loaded on first use (shared by the middleware and the login route)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._value: bytes | None = None

    @property
    def value(self) -> bytes:
        if self._value is None:
            self._value = load_session_key(self.settings, DataPaths(self.settings.data_dir))
        return self._value


def _signature(key: bytes, password: str, issued: str) -> str:
    message = b"median-session\0" + hashlib.sha256(password.encode()).digest() + issued.encode()
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def make_token(key: bytes, password: str, now: float | None = None) -> str:
    issued = str(int(now if now is not None else time.time()))
    return f"{issued}.{_signature(key, password, issued)}"


def token_valid(key: bytes, password: str, token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    issued, _, signature = token.partition(".")
    if not issued.isdigit():
        return False
    if not hmac.compare_digest(signature, _signature(key, password, issued)):
        return False
    age = (now if now is not None else time.time()) - int(issued)
    return -60 <= age < SESSION_SECONDS


def password_matches(expected: str, given: str) -> bool:
    return bool(expected) and hmac.compare_digest(expected.encode(), given.encode())


def safe_next(target: str | None) -> str:
    """Only local paths after login (no scheme, no host, no //)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


# Login slowdown ------------------------------------------------------------------------------


@dataclass
class _Failures:
    count: int
    last: float


class LoginLimiter:
    """Failed logins slow down progressively per client IP (in memory, per process).

    Before checking a password, a client with n earlier failures waits
    min(BASE * 2 ** (n - 1), MAX) seconds, one attempt at a time per IP. A success resets
    the count; failures are forgotten after FORGET seconds without a new one.
    """

    BASE = 1.0
    MAX = 30.0
    FORGET = 3600.0
    MAX_CLIENTS = 10_000

    def __init__(
        self,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.sleep = sleep
        self.clock = clock
        self._failures: OrderedDict[str, _Failures] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    def failures(self, ip: str) -> int:
        entry = self._failures.get(ip)
        if entry is None:
            return 0
        if self.clock() - entry.last > self.FORGET:
            del self._failures[ip]
            return 0
        return entry.count

    def delay(self, ip: str) -> float:
        n = self.failures(ip)
        return 0.0 if n == 0 else min(self.BASE * 2 ** (n - 1), self.MAX)

    def failed(self, ip: str) -> None:
        count = self.failures(ip) + 1
        self._failures[ip] = _Failures(count, self.clock())
        self._failures.move_to_end(ip)
        while len(self._failures) > self.MAX_CLIENTS:
            old, _ = self._failures.popitem(last=False)
            self._locks.pop(old, None)

    def succeeded(self, ip: str) -> None:
        self._failures.pop(ip, None)

    @asynccontextmanager
    async def attempt(self, ip: str):
        """Serialize the attempts of one IP and wait out its delay first."""
        lock = self._locks.setdefault(ip, asyncio.Lock())
        async with lock:
            wait = self.delay(ip)
            if wait:
                await self.sleep(wait)
            yield


# Middleware ----------------------------------------------------------------------------------


def _headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}


def same_origin(headers: dict[str, str]) -> bool:
    """Origin (or, without it, Referer) names the host this request was sent to."""
    host = headers.get("host", "").lower()
    source = headers.get("origin") or headers.get("referer")
    if not host or not source or source == "null":
        return False
    return urlsplit(source).netloc.lower() == host


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def session_cookie(headers: dict[str, str]) -> str | None:
    raw = headers.get("cookie")
    if not raw:
        return None
    cookie: SimpleCookie = SimpleCookie()
    try:
        cookie.load(raw)
    except Exception:
        return None
    morsel = cookie.get(COOKIE_NAME)
    return morsel.value if morsel else None


class AuthMiddleware:
    """Pure ASGI (request bodies stream through untouched, e.g. uploads)."""

    def __init__(self, app: ASGIApp, settings: Settings, key: SessionKey) -> None:
        self.app = app
        self.settings = settings
        self.key = key

    def authenticated(self, headers: dict[str, str]) -> bool:
        return token_valid(self.key.value, self.settings.app_password, session_cookie(headers))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = _headers(scope)
        method = scope["method"].upper()
        path = scope["path"]
        response: Response | None = None
        if method in UNSAFE_METHODS and not same_origin(headers):
            response = JSONResponse(
                {"detail": "cross-origin request refused (Origin or Referer must match Host)"},
                status_code=403,
            )
        elif self.settings.app_password and not is_public(path) and not self.authenticated(headers):
            response = self._login_required(scope, headers, method, path)
        if response is not None:
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _login_required(
        self, scope: Scope, headers: dict[str, str], method: str, path: str
    ) -> Response:
        query = scope.get("query_string", b"").decode("latin-1")
        target = path + (f"?{query}" if query else "")
        login = "/login?next=" + quote(target, safe="")
        htmx = headers.get("hx-request") == "true"
        if method in ("GET", "HEAD") and not htmx and not path.startswith("/api/"):
            return RedirectResponse(login, status_code=303)
        response = JSONResponse({"detail": "login required"}, status_code=401)
        if htmx:
            response.headers["HX-Redirect"] = login
        return response
