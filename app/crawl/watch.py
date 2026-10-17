"""Helpers for add-ons that emit files while a crawler subprocess is still running."""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from app.crawl.base import CrawlContext, SubprocessResult


class LineTail:
    """Reads the complete lines appended to a file since the last call."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0

    def read(self, final: bool = False) -> list[str]:
        """New complete lines; with final=True also a last line without a newline."""
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                data = fh.read()
        except FileNotFoundError:
            return []
        end = len(data) if final else data.rfind(b"\n") + 1
        self.offset += end
        text = data[:end].decode("utf-8", errors="replace")
        return [line.rstrip("\r") for line in text.split("\n") if line.strip()]


@dataclass
class StableFiles:
    """Finds media files in a directory tree that are ready to be emitted.

    A file is ready when its sidecar (sidecar_for(path)) exists and its size has not changed
    for stable_seconds. Partial downloads (partial_suffixes), sidecars and hidden files are
    never candidates. Each file is reported once.
    """

    root: Path
    sidecar_for: Callable[[Path], Path]
    stable_seconds: float = 2.0
    partial_suffixes: tuple[str, ...] = (".part",)
    sidecar_suffixes: tuple[str, ...] = (".json",)
    clock: Callable[[], float] = time.monotonic
    _seen: dict[Path, tuple[int, float]] = field(default_factory=dict)
    _done: set[Path] = field(default_factory=set)

    def candidates(self) -> Iterator[Path]:
        if not self.root.is_dir():
            return
        for path in sorted(self.root.rglob("*")):
            name = path.name
            if name.startswith(".") or path in self._done or not path.is_file():
                continue
            if name.endswith(self.partial_suffixes) or name.endswith(self.sidecar_suffixes):
                continue
            yield path

    def ready(self, final: bool = False) -> list[Path]:
        """Files ready now; with final=True every candidate that has its sidecar."""
        now = self.clock()
        out = []
        for path in self.candidates():
            if not self.sidecar_for(path).is_file():
                continue
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            previous = self._seen.get(path)
            if previous is None or previous[0] != size:
                self._seen[path] = (size, now)
                if not final:
                    continue
            elif not final and now - previous[1] < self.stable_seconds:
                continue
            self._done.add(path)
            out.append(path)
        return out

    def leftovers(self) -> list[Path]:
        """Candidates never reported (for example media without a sidecar)."""
        return list(self.candidates())


async def run_while_polling(
    ctx: CrawlContext,
    args: list[str],
    poll: Callable[[bool], Awaitable[None]],
    *,
    interval: float,
    timeout: float,  # noqa: ASYNC109 - passed on to run_subprocess
) -> SubprocessResult:
    """Run a subprocess through ctx and call poll(False) every interval while it runs.

    After it exits, poll(True) picks up whatever is left.
    """
    task = asyncio.create_task(ctx.run_subprocess(args, timeout=timeout))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=interval)
            await poll(False)
        result = task.result()
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    await poll(True)
    return result
