#!/usr/bin/env python3
"""End-to-end acceptance test (phase 9), run by make e2e.

Starts its own stack with fresh volumes and APP_PASSWORD set, drives it through the HTTP API in
the order of the phase 9 brief (steps a-j), prints PASS or FAIL per step with a reason, tears
its stacks down and exits non-zero on any failure. Standard library only: it runs on the host.

Backends:
- compose (default): Docker Compose projects median-e2e-a and median-e2e-b built from
  docker-compose.yml (no dev override), on ports E2E_PORT_A (18000) and E2E_PORT_B (18001).
- local (E2E_LOCAL=1, for sandboxes that cannot build the image): uvicorn and the worker run on
  this machine from the current Python (the project venv). E2E_PG_URL is a libpq URL of a role
  that may create databases (for example postgresql://median:median@host:5432/postgres); the
  databases median_e2e_a and median_e2e_b are dropped and recreated. pg_dump/pg_restore must be
  the server's major version.

Things the HTTP API cannot do are done where the app runs: SQL row counts for checks, deleting
and restoring an archive file (step h), make restore into the second stack (step i), the CLI
import (step j) and the simulated crawler ingest (step f) through scripts/e2e_helper.py.
E2E_KEEP=1 leaves the stacks running for inspection.
"""

from __future__ import annotations

import contextlib
import http.cookiejar
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "e2e-" + secrets.token_hex(8)
SECRET_KEY = secrets.token_hex(32)
DEMO_COUNT = 30
IMPORT_COUNT = int(os.environ.get("E2E_IMPORT_COUNT", "1000"))
LATENCY_LIMIT = 0.5


class StepFailed(Exception):
    pass


def check(condition: object, reason: str) -> None:
    if not condition:
        raise StepFailed(reason)


def log(message: str) -> None:
    print(f"  .. {message}", flush=True)


# --- HTTP ------------------------------------------------------------------------------------


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes, seconds: float) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        self.seconds = seconds

    def json(self) -> object:
        return json.loads(self.body)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class Client:
    """A browser-like client: cookie jar, Origin on every request, no automatic redirects."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), NoRedirect()
        )

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 60,
    ) -> Response:
        req = urllib.request.Request(self.base + path, data=body, method=method)  # noqa: S310
        req.add_header("Origin", self.base)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        start = time.perf_counter()
        try:
            with self.opener.open(req, timeout=timeout) as r:
                data = r.read()
                status, hdrs = r.status, dict(r.headers.items())
        except urllib.error.HTTPError as exc:
            data, status, hdrs = exc.read(), exc.code, dict(exc.headers.items())
        return Response(status, hdrs, data, time.perf_counter() - start)

    def get(self, url: str, **params: object) -> Response:
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        return self.request("GET", url)

    def get_json(self, url: str, **params: object) -> dict:
        r = self.get(url, **params)
        check(r.status == 200, f"GET {url} {params or ''} -> {r.status}: {r.text[:300]}")
        return r.json()  # type: ignore[return-value]

    def send_json(self, method: str, path: str, payload: object, expect: int | None = None) -> dict:
        r = self.request(
            method, path, json.dumps(payload).encode(), {"Content-Type": "application/json"}
        )
        ok = r.status == expect if expect else 200 <= r.status < 300
        check(ok, f"{method} {path} -> {r.status}: {r.text[:300]}")
        return r.json() if r.body else {}  # type: ignore[return-value]

    def post_json(self, path: str, payload: object, expect: int | None = None) -> dict:
        return self.send_json("POST", path, payload, expect)

    def post_form(self, path: str, fields: dict[str, str]) -> Response:
        body = urllib.parse.urlencode(fields).encode()
        return self.request(
            "POST", path, body, {"Content-Type": "application/x-www-form-urlencoded"}
        )

    def upload(self, files: list[tuple[str, bytes]], **fields: str) -> dict:
        boundary = uuid.uuid4().hex
        parts: list[bytes] = []
        for key, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n".encode()
            )
        for name, data in files:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
                f'filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                + data
                + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        r = self.request(
            "POST",
            "/api/uploads",
            b"".join(parts),
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        check(r.status == 202, f"POST /api/uploads -> {r.status}: {r.text[:300]}")
        return r.json()  # type: ignore[return-value]


def wait_for(what: str, probe: Callable[[], object], timeout: float, interval: float = 1.0):
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        last = probe()
        if last:
            return last
        time.sleep(interval)
    raise StepFailed(f"timed out after {timeout:.0f} s waiting for {what}")


# --- stacks ----------------------------------------------------------------------------------


def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=False, **kwargs)


class Stack:
    """One Median instance. Subclasses decide where it runs."""

    name: str
    base: str
    data_dir: str  # DATA_DIR as the app sees it
    scratch_dir: str  # a writable directory outside DATA_DIR, as the app sees it

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def app(self, args: list[str], stdin: bytes | None = None, timeout: float = 600) -> bytes:
        """Run a command where the app runs; return stdout; fail on a non-zero exit."""
        raise NotImplementedError

    def sql(self, query: str) -> str:
        raise NotImplementedError

    def fetch_data_file(self, rel: str, dest: Path) -> None:
        raise NotImplementedError

    def restore(self, dump: Path) -> None:
        raise NotImplementedError

    def logs(self) -> str:
        """The tail of the web and worker logs, printed when a step failed."""
        return ""

    def helper(self, args: list[str], stdin: bytes | None = None, timeout: float = 600) -> bytes:
        return self.app(["python", "scripts/e2e_helper.py", *args], stdin, timeout)

    def wait_healthy(self, timeout: float = 300) -> None:
        client = Client(self.base)

        def healthy() -> bool:
            try:
                return client.get("/healthz").status == 200
            except OSError:
                return False

        wait_for(f"{self.name} /healthz", healthy, timeout)


class ComposeStack(Stack):
    def __init__(self, name: str, port: int) -> None:
        self.name = name
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.data_dir = "/data"
        self.scratch_dir = "/tmp"  # noqa: S108 - inside the web container
        self.compose = ["docker", "compose", "-p", name, "-f", str(ROOT / "docker-compose.yml")]
        self.env = {
            **os.environ,
            "APP_PASSWORD": PASSWORD,
            "SECRET_KEY": SECRET_KEY,
            "WEB_PORT": str(port),
        }

    def _make(self, target: str, *extra: str) -> None:
        result = run(
            ["make", "-C", str(ROOT), target, f"COMPOSE={' '.join(self.compose)}", *extra],
            env=self.env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise StepFailed(
                f"make {target} ({self.name}): {(result.stdout + result.stderr)[-2000:]}"
            )

    def start(self) -> None:
        self.stop()
        self._make("up")
        self.wait_healthy()

    def stop(self) -> None:
        run([*self.compose, "down", "-v", "--remove-orphans"], env=self.env, capture_output=True)

    def app(self, args: list[str], stdin: bytes | None = None, timeout: float = 600) -> bytes:
        result = run(
            [*self.compose, "exec", "-T", "web", *args],
            env=self.env,
            input=stdin if stdin is not None else b"",
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise StepFailed(f"{' '.join(args)}: {result.stderr.decode()[-2000:]}")
        return result.stdout

    def sql(self, query: str) -> str:
        result = run(
            [
                *self.compose,
                "exec",
                "-T",
                "db",
                "sh",
                "-c",
                'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -qAt -c "$1"',
                "sh",
                query,
            ],
            env=self.env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise StepFailed(f"sql {query!r}: {result.stderr}")
        return result.stdout.strip()

    def fetch_data_file(self, rel: str, dest: Path) -> None:
        result = run(
            [*self.compose, "cp", f"worker:{self.data_dir}/{rel}", str(dest)],
            env=self.env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise StepFailed(f"docker compose cp {rel}: {result.stderr}")

    def logs(self) -> str:
        result = run(
            [*self.compose, "logs", "--no-color", "--tail=150", "web", "worker"],
            env=self.env,
            capture_output=True,
            text=True,
        )
        return result.stdout + result.stderr

    def restore(self, dump: Path) -> None:
        self._make("restore", f"FILE={dump}", "CONFIRM=yes")
        self.wait_healthy()


class LocalStack(Stack):
    def __init__(self, name: str, port: int, admin_url: str, workdir: Path) -> None:
        self.name = name
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.db = name.replace("-", "_")
        self.admin_url = admin_url
        parts = urllib.parse.urlsplit(admin_url)
        self.database_url = urllib.parse.urlunsplit(parts._replace(path="/" + self.db))
        self.root = workdir / name
        self.data_dir = str(self.root / "data")
        self.scratch_dir = str(self.root)
        self.env = {
            **os.environ,
            "DATABASE_URL": self.database_url,
            "TEST_DATABASE_URL": self.database_url + "_unused",
            "DATA_DIR": self.data_dir,
            "APP_PASSWORD": PASSWORD,
            "SECRET_KEY": SECRET_KEY,
            "MEDIAN_SKIP_TOOLS": "1",
            "PYTHONPATH": str(ROOT),
            # app.worker execs the procrastinate script of the venv.
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        }
        self.procs: dict[str, subprocess.Popen] = {}

    def _psql_admin(self, query: str) -> None:
        result = run(
            ["psql", self.admin_url, "-v", "ON_ERROR_STOP=1", "-qAt", "-c", query],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise StepFailed(f"psql: {result.stderr}")

    def _spawn(self, role: str) -> None:
        if role == "web":
            args = [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ]
        else:
            args = [sys.executable, "-m", "app.worker", "--concurrency=4"]
        out = open(self.root / f"{role}.log", "ab")  # noqa: SIM115 - lives as long as the process
        self.procs[role] = subprocess.Popen(
            args, cwd=ROOT, env=self.env, stdout=out, stderr=subprocess.STDOUT
        )

    def _halt(self, role: str) -> None:
        proc = self.procs.pop(role, None)
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def start(self) -> None:
        self.stop()
        shutil.rmtree(self.root, ignore_errors=True)
        Path(self.data_dir).mkdir(parents=True)
        self._psql_admin(f'DROP DATABASE IF EXISTS "{self.db}" WITH (FORCE)')
        self._psql_admin(f'CREATE DATABASE "{self.db}"')
        self.app([sys.executable, "-m", "app.db.migrate"])
        self._spawn("web")
        self._spawn("worker")
        self.wait_healthy()

    def stop(self) -> None:
        for role in list(self.procs):
            self._halt(role)

    def teardown(self) -> None:
        self.stop()
        with contextlib.suppress(StepFailed):
            self._psql_admin(f'DROP DATABASE IF EXISTS "{self.db}" WITH (FORCE)')
        shutil.rmtree(self.root, ignore_errors=True)

    def app(self, args: list[str], stdin: bytes | None = None, timeout: float = 600) -> bytes:
        if args[0] == "python":
            args = [sys.executable, *args[1:]]
        result = run(
            args,
            cwd=ROOT,
            env=self.env,
            input=stdin if stdin is not None else b"",
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise StepFailed(f"{' '.join(args)}: {result.stderr.decode()[-2000:]}")
        return result.stdout

    def sql(self, query: str) -> str:
        result = run(
            ["psql", self.database_url, "-v", "ON_ERROR_STOP=1", "-qAt", "-c", query],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise StepFailed(f"sql {query!r}: {result.stderr}")
        return result.stdout.strip()

    def fetch_data_file(self, rel: str, dest: Path) -> None:
        shutil.copyfile(Path(self.data_dir) / rel, dest)

    def logs(self) -> str:
        tails = []
        for role in ("web", "worker"):
            path = self.root / f"{role}.log"
            if path.is_file():
                lines = path.read_text(errors="replace").splitlines()[-150:]
                tails.append(f"--- {role}.log\n" + "\n".join(lines))
        return "\n".join(tails)

    def restore(self, dump: Path) -> None:
        # Like make restore: stop web and worker, restore from the file, start them again.
        self.stop()
        with dump.open("rb") as f:
            self.app([sys.executable, "-m", "app.cli", "restore", "--yes", "-"], f.read())
        self._spawn("web")
        self._spawn("worker")
        self.wait_healthy()


# --- the scenario ----------------------------------------------------------------------------


class Scenario:
    def __init__(self, a: Stack, b: Stack, workdir: Path) -> None:
        self.a, self.b = a, b
        self.http = Client(a.base)
        self.workdir = workdir
        self.ids: dict[str, object] = {}

    # helpers

    def wait_batch(self, batch_id: str, timeout: float = 120) -> dict:
        def done() -> dict | None:
            body = self.http.get_json(f"/api/uploads/{batch_id}")
            return body if body["done"] else None

        return wait_for(f"batch {batch_id}", done, timeout)

    def wait_run(self, run_id: int, timeout: float = 300) -> dict:
        def finished() -> dict | None:
            body = self.http.get_json(f"/api/jobs/{run_id}")
            if body["status"] in ("queued", "running"):
                return None
            if body["kind"] == "crawl" and body["stats"].get("pending", 0):
                return None
            return body

        return wait_for(f"run {run_id}", finished, timeout)

    def queue(self, **params: object) -> dict:
        return self.http.get_json("/api/review/queue", limit=500, **params)

    def search(self, q: str) -> dict:
        return self.http.get_json("/api/search", q=q, limit=200)

    def file_tags(self, file_id: int) -> tuple[set[str], set[str]]:
        body = self.http.get_json(f"/api/files/{file_id}/tags")
        return {t["name"] for t in body["active"]}, {t["name"] for t in body["removed"]}

    def statuses(self) -> dict[str, int]:
        rows = self.a.sql("SELECT status, count(*) FROM files GROUP BY 1")
        return {s: int(n) for s, n in (line.split("|") for line in rows.splitlines() if line)}

    def run_from_settings(self, kind: str, **fields: str) -> int:
        r = self.http.post_form(f"/settings/run/{kind}", fields)
        location = r.headers.get("location", r.headers.get("Location", ""))
        check(r.status == 303 and "/jobs/" in location, f"Run now {kind}: {r.status} {location}")
        return int(location.rsplit("/", 1)[1])

    # steps

    def step_a(self) -> str:
        anon = Client(self.a.base)
        r = anon.get("/library")
        check(
            r.status == 303 and "/login" in r.headers.get("location", ""),
            f"/library without a session: {r.status}",
        )
        r = anon.get("/api/review/queue")
        check(r.status == 401, f"API without a session: {r.status}")
        r = self.http.post_form("/login", {"password": "wrong", "next": "/"})
        check(r.status == 401, f"wrong password: {r.status}")
        r = self.http.post_form("/login", {"password": PASSWORD, "next": "/library"})
        check(r.status == 303 and r.headers.get("location") == "/library", f"login: {r.status}")
        check(any(c.name == "median_session" for c in self.http.jar), "no session cookie")
        check(self.http.get("/library").status == 200, "library after login")
        health = self.http.get_json("/healthz")
        check(health == {"status": "ok", "database": "ok"}, f"healthz: {health}")
        return (
            f"pages redirect and the API answers 401 without a session; logged in; healthz {health}"
        )

    def step_b(self) -> str:
        animals = self.http.post_json("/api/folders", {"name": "Animals"})["id"]
        cats = self.http.post_json("/api/folders", {"name": "Cats", "parent_id": animals})["id"]
        resolved = self.http.get_json("/api/folders/resolve", path="Animals/Cats")
        check(resolved["id"] == cats, f"resolve Animals/Cats: {resolved}")
        source = self.http.post_json(
            "/api/sources",
            {
                "name": "E2E demo",
                "addon_key": "demo",
                "config": {"count": DEMO_COUNT, "seed": 1, "tags": ["demo", "artist:test"]},
                "default_folder_id": animals,
            },
        )
        run_id = self.http.post_json(f"/api/sources/{source['id']}/run", {})["run_id"]
        run_ = self.wait_run(run_id)
        stats = run_["stats"]
        check(run_["status"] == "succeeded", f"run {run_id}: {run_['status']} {run_['error']}")
        check(stats["emitted"] == DEMO_COUNT and stats["new"] == DEMO_COUNT, f"run stats {stats}")
        queue = self.queue(source_key="demo")
        check(queue["total"] == DEMO_COUNT, f"queue total {queue['total']}")
        items = queue["items"]
        for item in items:
            check(item.get("default_folder_id") == animals, f"default folder of {item['id']}")
        active, _ = self.file_tags(items[0]["id"])
        check({"demo", "artist:test"} <= active, f"crawler tags of {items[0]['id']}: {active}")
        check(self.statuses() == {"pending_review": DEMO_COUNT}, f"statuses {self.statuses()}")
        self.ids.update(
            animals=animals, cats=cats, source=source["id"], pending=[i["id"] for i in items]
        )
        return (
            f"folders Animals ({animals}) and Animals/Cats ({cats}); demo run {run_id} "
            f"emitted {stats['emitted']}, new {stats['new']}; {DEMO_COUNT} pending with tags "
            f"demo, artist:test and default folder Animals"
        )

    def step_c(self) -> str:
        ids: list[int] = self.ids["pending"]  # type: ignore[assignment]
        cat_ids, animal_ids, discard_ids = ids[:10], ids[10:20], ids[20:30]
        untagged = cat_ids[0]
        reply = self.http.post_json(
            "/api/review/archive",
            {
                "file_ids": cat_ids,
                "folder_id": self.ids["cats"],
                "add_tags": ["cat"],
                "remove_tags_per_file": {str(untagged): ["demo"]},
            },
        )
        check(reply["ok"] and all(r["ok"] for r in reply["results"]), f"archive cats: {reply}")
        reply = self.http.post_json(
            "/api/review/archive", {"file_ids": animal_ids, "folder_id": self.ids["animals"]}
        )
        check(reply["ok"], f"archive animals: {reply}")
        reply = self.http.post_json("/api/review/discard", {"file_ids": discard_ids})
        check(reply["ok"] and len(reply["action_ids"]) == 10, f"discard: {reply}")
        undo_target = discard_ids[0]
        action = next(r["action_id"] for r in reply["results"] if r["file_id"] == undo_target)
        undo = self.http.post_json("/api/review/undo", {"action_ids": [action]})
        check(undo["ok"], f"undo: {undo}")
        check(self.statuses().get("pending_review") == 1, f"after undo {self.statuses()}")
        staged = self.a.sql(f"SELECT staging_path IS NOT NULL FROM files WHERE id = {undo_target}")
        check(staged == "t", f"file {undo_target} has no staging path after undo")
        reply = self.http.post_json("/api/review/discard", {"file_ids": [undo_target]})
        check(reply["ok"], f"discard again: {reply}")
        expected = {"archived": 20, "discarded": 10}
        check(self.statuses() == expected, f"statuses {self.statuses()}")
        active, removed = self.file_tags(untagged)
        check(
            "demo" in removed and "demo" not in active and "cat" in active,
            f"file {untagged}: active {active}, removed {removed}",
        )
        folders = self.a.sql(
            "SELECT folder_id, count(*) FROM files WHERE status = 'archived' GROUP BY 1 ORDER BY 1"
        )
        check(folders == f"{self.ids['animals']}|10\n{self.ids['cats']}|10", f"folders {folders}")
        self.ids.update(untagged=untagged, discarded=discard_ids, cat_ids=cat_ids)
        return (
            "10 archived into Animals/Cats with cat (demo removed from file "
            f"{untagged}), 10 into Animals, 10 discarded; undo of the discard of "
            f"{undo_target} put it back in staging, discarded again"
        )

    def step_d(self) -> str:
        before = self.statuses()
        run_id = self.http.post_json(f"/api/sources/{self.ids['source']}/run", {})["run_id"]
        run_ = self.wait_run(run_id)
        stats = run_["stats"]
        check(run_["status"] == "succeeded", f"run {run_id}: {run_['status']}")
        check(
            stats["skipped_known_remote"] == DEMO_COUNT and stats["new"] == 0,
            f"second run stats {stats}",
        )
        check(self.statuses() == before, f"statuses changed: {before} -> {self.statuses()}")
        return f"run {run_id}: skipped_known_remote {stats['skipped_known_remote']}, new 0"

    def step_e(self) -> str:
        discarded: list[int] = self.ids["discarded"]  # type: ignore[assignment]
        exact_id, resized_id = discarded[1], discarded[2]
        r = self.http.get(f"/media/{exact_id}/original")
        check(r.status == 200, f"original of discarded {exact_id}: {r.status}")
        exact = r.body
        batch = self.wait_batch(self.http.upload([("copy.png", exact)])["batch_id"])
        check(
            batch["counts"]["duplicate_exact"] == 1 and batch["counts"]["new"] == 0,
            f"exact copy: {batch['counts']}",
        )
        check(self.statuses()["discarded"] == 10, "exact copy changed a status")

        r = self.http.get(f"/media/{resized_id}/original")
        check(r.status == 200, f"original of discarded {resized_id}: {r.status}")
        resized = self.a.helper(["resize", "50"], r.body)
        batch = self.wait_batch(self.http.upload([("resized.png", resized)])["batch_id"])
        check(batch["counts"]["new"] == 1, f"resized copy: {batch['counts']}")
        new_id = batch["files"][0]["file_id"]
        similar = self.http.get_json(f"/api/files/{new_id}/similar")
        matches = [p for p in similar["similar"] if p["file_id"] == resized_id]
        check(matches and matches[0]["status"] == "discarded", f"similar of {new_id}: {similar}")
        queue = self.queue(similar_to_discarded="true")
        check([i["id"] for i in queue["items"]] == [new_id], f"similar_to_discarded queue {queue}")

        batch = self.wait_batch(self.http.upload([("copy.png", exact)], force="1")["batch_id"])
        check(batch["counts"]["forced_reingest"] == 1, f"forced copy: {batch['counts']}")
        status = self.a.sql(f"SELECT status FROM files WHERE id = {exact_id}")
        check(status == "pending_review", f"forced file {exact_id} is {status}")
        self.ids.update(resized_new=new_id, forced=exact_id)
        return (
            f"exact copy of {exact_id} duplicate_exact; 50% copy of {resized_id} is new file "
            f"{new_id}, pending, similar to discarded {resized_id} (distance "
            f"{matches[0].get('distance')}); forced copy: forced_reingest, {exact_id} pending"
        )

    def step_f(self) -> str:
        target: int = self.ids["untagged"]  # type: ignore[assignment]
        r = self.http.get(f"/media/{target}/original")
        check(r.status == 200, f"original of {target}: {r.status}")
        out = self.a.helper(["crawler-ingest", "demo", f"e2e-dup-{target}", "demo,extra"], r.body)
        batch = self.wait_batch(out.decode().strip().splitlines()[-1])
        check(batch["counts"]["duplicate_exact"] == 1, f"crawler duplicate: {batch['counts']}")
        active, removed = self.file_tags(target)
        check("demo" in removed and "demo" not in active, f"demo came back: active {active}")
        check("extra" in active, f"new crawler tag not merged: {active}")
        sources = self.a.sql(f"SELECT count(*) FROM file_sources WHERE file_id = {target}")
        check(sources == "2", f"file_sources of {target}: {sources}")
        return (
            f"duplicate crawler ingest of file {target} with tags demo, extra: duplicate_exact, "
            "demo stays removed, extra added, second file_sources row"
        )

    def step_g(self) -> str:
        cats = self.search('cat folder:"Animals"')
        check(cats["total"] == 10, f'cat folder:"Animals" -> {cats["total"]}')
        check({i["id"] for i in cats["items"]} == set(self.ids["cat_ids"]), "wrong cat files")
        others = self.search('-cat folder:"Animals"')
        check(others["total"] == 10, f'-cat folder:"Animals" -> {others["total"]}')
        discarded = self.search("status:discarded")
        expected = self.statuses()["discarded"]
        check(
            expected == 9 and discarded["total"] == expected,
            f"status:discarded -> {discarded['total']}, expected 9 ({expected} in the database)",
        )
        query = 'cat folder:"Animals" type:image'
        saved = self.http.post_json("/api/saved-searches", {"name": "E2E cats", "query": query})
        reopened = self.http.get_json(f"/api/saved-searches/{saved['id']}")
        again = self.search(reopened["query"])
        check(
            again["total"] == 10
            and [i["id"] for i in again["items"]] == [i["id"] for i in self.search(query)["items"]],
            "saved search differs",
        )
        return (
            f'cat folder:"Animals" 10, -cat folder:"Animals" 10, status:discarded '
            f"{discarded['total']}; saved search {saved['id']} re-ran with 10 results"
        )

    def integrity(self, verify: bool) -> dict:
        run_id = self.run_from_settings("integrity", **({"verify_hashes": "1"} if verify else {}))
        run_ = self.wait_run(run_id)
        check(run_["status"] == "succeeded", f"integrity run {run_id}: {run_}")
        return run_

    def step_h(self) -> str:
        run_ = self.integrity(verify=True)
        check(
            run_["stats"]["total_problems"] == 0,
            f"baseline integrity: {run_['stats']['counts']} {run_['stats']['problems']}",
        )
        victim = int(
            self.a.sql("SELECT id FROM files WHERE status = 'archived' ORDER BY id LIMIT 1")
        )
        rel = self.a.sql(
            "SELECT 'archive/' || substr(h, 1, 2) || '/' || substr(h, 3, 2) || '/' || h || "
            f"coalesce('.' || ext, '') FROM (SELECT encode(sha256, 'hex') AS h, ext FROM files "
            f"WHERE id = {victim}) f"
        )
        r = self.http.get(f"/media/{victim}/original")
        check(r.status == 200, f"original of {victim}: {r.status}")
        original = r.body
        path = f"{self.a.data_dir}/{rel}"
        self.a.app(["rm", path])
        run_ = self.integrity(verify=False)
        problems = run_["stats"]["problems"]
        check(
            run_["stats"]["total_problems"] == 1
            and problems.get("missing_archive") == [{"file_id": victim, "path": rel}],
            f"after deleting {rel}: {run_['stats']['counts']} {problems}",
        )
        self.a.helper(["write", path], original)
        run_ = self.integrity(verify=True)
        check(run_["stats"]["total_problems"] == 0, f"after restoring: {run_['stats']['problems']}")
        return (
            f"baseline clean (verify hashes); deleting {rel} reported exactly file {victim} as "
            "missing_archive; after writing it back the check is clean again"
        )

    TABLES = ("files", "file_tags", "folders", "file_sources")

    def counts(self, stack: Stack) -> dict[str, int]:
        return {t: int(stack.sql(f"SELECT count(*) FROM {t}")) for t in self.TABLES}

    def step_i(self) -> str:
        before = self.counts(self.a)
        run_id = self.run_from_settings("backup")
        run_ = self.wait_run(run_id)
        check(run_["status"] == "succeeded", f"backup run {run_id}: {run_}")
        rel = run_["stats"]["file"]
        dump = self.workdir / Path(rel).name
        self.a.fetch_data_file(rel, dump)
        check(dump.stat().st_size > 0, "empty dump")
        log(f"starting the second stack {self.b.name}")
        self.b.start()
        check(self.counts(self.b)["files"] == 0, "the second stack is not empty")
        self.b.restore(dump)
        after = self.counts(self.b)
        check(after == before, f"row counts differ: {before} (A) vs {after} (B)")
        other = Client(self.b.base)
        r = other.post_form("/login", {"password": PASSWORD, "next": "/"})
        check(r.status == 303, f"login on {self.b.name}: {r.status}")
        res = other.get_json("/api/search", q='cat folder:"Animals"')
        check(res["total"] == 10, f"search on the restored stack: {res['total']}")
        return f"backup {rel} restored into {self.b.name}: counts {after} match; search works there"

    def step_j(self) -> str:
        target = self.a.scratch_dir + "/e2e-import"
        self.a.helper(["generate", target, str(IMPORT_COUNT)], timeout=900)
        out = self.a.app(["python", "-m", "app.cli", "import", target], timeout=900).decode()
        line = next((ln for ln in out.splitlines() if ln.startswith("batch ")), "")
        check(f"queued {IMPORT_COUNT} file" in line, f"import output: {out[-500:]}")
        batch_id = line.split()[1].rstrip(":")
        started = time.monotonic()
        batch = self.wait_batch(batch_id, timeout=1800)
        took = time.monotonic() - started
        check(batch["counts"]["new"] == IMPORT_COUNT, f"import batch: {batch['counts']}")
        queue = self.http.get("/api/review/queue")
        check(queue.status == 200, f"review queue: {queue.status}")
        search = self.http.get("/api/search", q="status:pending")
        check(search.status == 200, f"search: {search.status}")
        total_q, total_s = queue.json()["total"], search.json()["total"]  # type: ignore[index]
        check(
            total_q == IMPORT_COUNT + 2 and total_s == IMPORT_COUNT + 2,
            f"queue total {total_q}, search total {total_s}",
        )
        page = self.http.get("/search", q="status:pending")
        check(page.status == 200, f"/search page: {page.status}")
        timings = {
            "review queue API": queue.seconds,
            "search API": search.seconds,
            "search page": page.seconds,
        }
        shown = ", ".join(f"{k} {v * 1000:.0f} ms" for k, v in timings.items())
        check(all(v < LATENCY_LIMIT for v in timings.values()), f"too slow: {shown}")
        return (
            f"{IMPORT_COUNT} images imported and processed in {took:.0f} s; {total_q} pending; "
            f"first pages: {shown}"
        )

    STEPS = (
        ("a", "login and healthz"),
        ("b", "folders, demo source, first run"),
        ("c", "archive, discard, undo"),
        ("d", "second demo run"),
        ("e", "exact, resized and forced uploads"),
        ("f", "duplicate crawler ingest keeps a removed tag removed"),
        ("g", "search and saved search"),
        ("h", "integrity check"),
        ("i", "backup and restore into a second stack"),
        ("j", f"import {IMPORT_COUNT} images, review queue and search latency"),
    )


def main() -> int:
    local = os.environ.get("E2E_LOCAL") == "1"
    keep = os.environ.get("E2E_KEEP") == "1"
    workdir = Path(tempfile.mkdtemp(prefix="median-e2e-"))
    port_a = int(os.environ.get("E2E_PORT_A", "18000"))
    port_b = int(os.environ.get("E2E_PORT_B", "18001"))
    if local:
        admin = os.environ.get("E2E_PG_URL")
        if not admin:
            print("E2E_LOCAL=1 needs E2E_PG_URL", file=sys.stderr)
            return 2
        a: Stack = LocalStack("median-e2e-a", port_a, admin, workdir)
        b: Stack = LocalStack("median-e2e-b", port_b, admin, workdir)
    else:
        a = ComposeStack("median-e2e-a", port_a)
        b = ComposeStack("median-e2e-b", port_b)
    failed = False
    started = time.monotonic()
    try:
        print(f"starting {a.name} ({'local' if local else 'compose'}) on {a.base}", flush=True)
        try:
            a.start()
        except StepFailed as exc:
            failed = True
            print(f"FAIL setup: {exc}", flush=True)
            return 1
        scenario = Scenario(a, b, workdir)
        for key, title in Scenario.STEPS:
            if failed:
                print(f"SKIP {key}. {title}")
                continue
            t0 = time.monotonic()
            try:
                detail = getattr(scenario, f"step_{key}")()
            except StepFailed as exc:
                failed = True
                print(f"FAIL {key}. {title}: {exc}", flush=True)
            except Exception as exc:
                failed = True
                print(f"FAIL {key}. {title}: {type(exc).__name__}: {exc}", flush=True)
            else:
                print(f"PASS {key}. {title} ({time.monotonic() - t0:.0f} s): {detail}", flush=True)
    finally:
        if failed:
            for stack in (a, b):
                text = stack.logs()
                if text:
                    print(f"=== logs of {stack.name} ===\n{text}")
        if keep:
            print(f"E2E_KEEP=1: {a.base} and {b.base} are still running (password {PASSWORD})")
        else:
            for stack in (a, b):
                if isinstance(stack, LocalStack):
                    stack.teardown()
                else:
                    stack.stop()
            shutil.rmtree(workdir, ignore_errors=True)
    verdict = "FAILED" if failed else "PASSED"
    print(f"e2e {verdict} in {time.monotonic() - started:.0f} s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
