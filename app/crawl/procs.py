"""ctx.run_subprocess: argument lists only, own process group, limited environment."""

import asyncio
import contextlib
import logging
import os
import signal
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path

from app.crawl.base import SubprocessResult

TAIL_LINES = 50
KILL_GRACE_SECONDS = 2.0
DRAIN_SECONDS = 5.0


def subprocess_env(home: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """PATH, LANG and HOME (the source's state_dir), plus explicitly passed variables."""
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "HOME": str(home),
    }
    env.update(extra or {})
    return env


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


async def _stop_group(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the whole process group, SIGKILL it after a short grace period."""
    _signal_group(proc.pid, signal.SIGTERM)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_SECONDS)
    # Children may outlive the leader (or ignore SIGTERM): kill whatever is left.
    _signal_group(proc.pid, signal.SIGKILL)
    await proc.wait()


async def run_subprocess(
    args: Sequence[str | os.PathLike[str]],
    *,
    log: logging.Logger,
    cancel_event: asyncio.Event,
    timeout: float,  # noqa: ASYNC109 - enforced with asyncio.wait
    home: Path,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> SubprocessResult:
    """Run args[0] with args[1:] in its own process group and stream its output to log.

    The whole process group is stopped on timeout, on cancel_event and when the calling task
    is cancelled. Returns the exit code and the last 50 output lines.
    """
    if isinstance(args, str | bytes):
        raise TypeError("run_subprocess takes an argument list, not a string")
    argv = [os.fspath(a) for a in args]
    if not argv:
        raise ValueError("empty argument list")
    log.info("$ %s", " ".join(repr(a) if " " in a else a for a in argv))
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=subprocess_env(home, env),
        start_new_session=True,  # setsid: the child leads a new process group
    )
    tail: deque[str] = deque(maxlen=TAIL_LINES)

    async def pump() -> None:
        assert proc.stdout is not None
        while line := await proc.stdout.readline():
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            tail.append(text)
            log.info("| %s", text)

    pump_task = asyncio.create_task(pump())
    cancel_task = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait(
            {pump_task, cancel_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if pump_task in done:
            pump_task.result()
            code = await proc.wait()
            log.info("exit code %s", code)
            return SubprocessResult(code, list(tail))
        cancelled = cancel_task in done
        log.warning(
            "stopping process group %s (%s)",
            proc.pid,
            "run cancelled" if cancelled else f"timeout after {timeout:g} s",
        )
        await _stop_group(proc)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(pump_task), timeout=DRAIN_SECONDS)
        return SubprocessResult(proc.returncode, list(tail), not cancelled, cancelled)
    finally:
        cancel_task.cancel()
        if proc.returncode is None:
            # The caller was cancelled (for example by the runner): no await is safe here.
            _signal_group(proc.pid, signal.SIGKILL)
        if not pump_task.done():
            pump_task.cancel()
