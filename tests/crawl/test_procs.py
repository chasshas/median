import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import pytest

from app.crawl.procs import PASSTHROUGH_VARIABLES, TAIL_LINES, run_subprocess


@pytest.fixture
def job_log(tmp_path: Path):
    path = tmp_path / "job.log"
    log = logging.getLogger(f"test.procs.{id(path)}")
    log.setLevel(logging.INFO)
    handler = logging.FileHandler(path)
    log.addHandler(handler)
    yield log, path
    log.removeHandler(handler)
    handler.close()


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); it is dead for our purposes.
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except FileNotFoundError:
        return False


async def test_output_is_streamed_and_tail_returned(job_log, tmp_path: Path) -> None:
    log, path = job_log
    script = (
        "import sys\nfor i in range(80): print('line', i)\n"
        "print('oops', file=sys.stderr)\nsys.exit(3)"
    )
    result = await run_subprocess(
        [sys.executable, "-u", "-c", script],
        log=log,
        cancel_event=asyncio.Event(),
        timeout=30,
        home=tmp_path,
    )

    assert result.returncode == 3
    assert not result.ok
    assert len(result.tail) == TAIL_LINES
    assert result.tail[-2:] == ["line 79", "oops"]
    text = path.read_text()
    assert "| line 0\n" in text
    assert "| oops\n" in text
    assert "exit code 3" in text


async def test_argument_is_passed_literally(job_log, tmp_path: Path) -> None:
    log, _ = job_log
    marker = tmp_path / "marker"
    marker.write_text("still here")
    dangerous = f"; rm -rf {marker} && echo pwned"
    result = await run_subprocess(
        [sys.executable, "-c", "import sys; print(repr(sys.argv[1]))", dangerous],
        log=log,
        cancel_event=asyncio.Event(),
        timeout=30,
        home=tmp_path,
    )

    assert result.returncode == 0
    assert result.tail == [repr(dangerous)]
    assert marker.read_text() == "still here"


async def test_string_args_are_refused(job_log, tmp_path: Path) -> None:
    log, _ = job_log
    with pytest.raises(TypeError):
        await run_subprocess(
            "echo hi", log=log, cancel_event=asyncio.Event(), timeout=5, home=tmp_path
        )


async def test_environment_is_limited(job_log, tmp_path: Path, monkeypatch) -> None:
    log, _ = job_log
    monkeypatch.setenv("MEDIAN_SECRET_TOKEN", "do-not-leak")
    for name in PASSTHROUGH_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    home = tmp_path / "state"
    home.mkdir()
    result = await run_subprocess(
        [sys.executable, "-c", "import os, json; print(json.dumps(dict(os.environ)))"],
        log=log,
        cancel_event=asyncio.Event(),
        timeout=30,
        home=home,
        env={"EXTRA": "1"},
    )
    import json

    env = json.loads(result.tail[-1])
    env.pop("LC_CTYPE", None)  # macOS-style injection by Python itself, if any
    assert set(env) == {"PATH", "LANG", "HOME", "HTTPS_PROXY", "EXTRA"}
    assert env["HTTPS_PROXY"] == "http://proxy.example:3128"
    assert env["HOME"] == str(home)
    assert env["EXTRA"] == "1"
    assert env["PATH"] == os.environ["PATH"]


async def test_cwd_is_used(job_log, tmp_path: Path) -> None:
    log, _ = job_log
    result = await run_subprocess(
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        log=log,
        cancel_event=asyncio.Event(),
        timeout=30,
        home=tmp_path,
        cwd=tmp_path,
    )
    assert result.tail == [str(tmp_path)]


def _read_pid(path: Path) -> int | None:
    text = path.read_text().strip() if path.exists() else ""
    return int(text) if text else None


async def _child_pid(path: Path) -> int:
    for _ in range(100):
        pid = _read_pid(path)
        if pid is not None:
            return pid
        await asyncio.sleep(0.05)
    raise AssertionError("child pid not written")


async def test_timeout_kills_the_process_group(job_log, tmp_path: Path) -> None:
    log, path = job_log
    pidfile = tmp_path / "child.pid"
    started = time.monotonic()
    result = await run_subprocess(
        ["sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait"],
        log=log,
        cancel_event=asyncio.Event(),
        timeout=1,
        home=tmp_path,
    )
    elapsed = time.monotonic() - started

    assert result.timed_out and not result.cancelled
    assert elapsed < 6
    child = await _child_pid(pidfile)
    await asyncio.sleep(0.2)
    assert not alive(child)
    assert "timeout after 1 s" in path.read_text()


async def test_cancel_event_kills_the_process_group(job_log, tmp_path: Path) -> None:
    log, _ = job_log
    pidfile = tmp_path / "child.pid"
    cancel = asyncio.Event()
    task = asyncio.create_task(
        run_subprocess(
            ["sh", "-c", f"trap '' TERM; sleep 60 & echo $! > {pidfile}; wait"],
            log=log,
            cancel_event=cancel,
            timeout=3600,
            home=tmp_path,
        )
    )
    child = await _child_pid(pidfile)
    cancel.set()
    result = await asyncio.wait_for(task, timeout=8)

    assert result.cancelled and not result.timed_out
    await asyncio.sleep(0.2)
    assert not alive(child)


async def test_task_cancellation_kills_the_process_group(job_log, tmp_path: Path) -> None:
    log, _ = job_log
    pidfile = tmp_path / "child.pid"
    task = asyncio.create_task(
        run_subprocess(
            ["sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait"],
            log=log,
            cancel_event=asyncio.Event(),
            timeout=3600,
            home=tmp_path,
        )
    )
    child = await _child_pid(pidfile)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.3)
    assert not alive(child)
