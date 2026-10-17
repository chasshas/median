"""The add-on interface: Addon, its config model, CrawledItem and CrawlContext (section 11)."""

import asyncio
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

KEY_PATTERN = re.compile(r"^[a-z0-9_]{1,50}$")
RESERVED_KEYS = frozenset({"upload"})
SECRET_FILE_FORMAT = "secret-file"  # noqa: S105 - a JSON schema format name
DEFAULT_SUBPROCESS_TIMEOUT = 3600.0


class SecretFileMarker:
    """Marks a config field whose value is a secret file (cookies, credentials).

    The file is uploaded on the source form, stored as DATA_DIR/secrets/<source_id>/<field>
    with mode 0600, and the add-on receives its path (None when no file was uploaded).
    """

    def __repr__(self) -> str:
        return "SecretFile"


SecretFile = Annotated[
    Path | None,
    SecretFileMarker(),
    WithJsonSchema({"type": "string", "format": SECRET_FILE_FORMAT}),
]


def secret_fields(model: type[BaseModel]) -> list[str]:
    """Names of the config fields declared as SecretFile."""
    return [
        name
        for name, info in model.model_fields.items()
        if any(isinstance(m, SecretFileMarker) for m in info.metadata)
    ]


class EmptyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Addon:
    """Base class of every add-on. Subclasses set the class attributes and implement run().

    run() gets a CrawlContext and a validated instance of ConfigModel. It emits files with
    await ctx.emit(CrawledItem(...)) and should check ctx.is_cancelled() between items.
    """

    key: ClassVar[str] = ""
    name: ClassVar[str] = ""
    version: ClassVar[str] = "0"
    description: ClassVar[str] = ""
    ConfigModel: ClassVar[type[BaseModel]] = EmptyConfig

    async def run(self, ctx: "CrawlContext", config: Any) -> None:
        raise NotImplementedError


class CrawledItem(BaseModel):
    """One file found by an add-on. path must be a file inside ctx.work_dir."""

    path: Path
    remote_id: str | None = None
    url: str | None = None
    title: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    filename: str | None = None  # original file name; defaults to path.name


@dataclass(frozen=True)
class SubprocessResult:
    returncode: int | None
    tail: list[str]  # the last output lines (stdout and stderr merged)
    timed_out: bool = False
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled


class CrawlBackend(Protocol):
    """What the runner provides behind a CrawlContext."""

    async def emit(self, item: CrawledItem) -> None: ...

    async def is_known(self, remote_id: str) -> bool: ...

    async def known_remote_ids(self) -> set[str]: ...


class CrawlContext:
    """Everything an add-on may use during one run."""

    def __init__(
        self,
        *,
        work_dir: Path,
        state_dir: Path,
        secrets_dir: Path,
        log: logging.Logger,
        backend: CrawlBackend,
        cancel_event: asyncio.Event,
        source_id: int | None = None,
        job_run_id: int | None = None,
    ) -> None:
        self.work_dir = work_dir
        self.state_dir = state_dir
        self.secrets_dir = secrets_dir
        self.log = log
        self.source_id = source_id
        self.job_run_id = job_run_id
        self._backend = backend
        self._cancel = cancel_event
        self.emitted = 0

    @property
    def logger(self) -> logging.Logger:
        return self.log

    async def emit(self, item: CrawledItem) -> None:
        """Hand a file in work_dir to the ingest service (it is moved into staging)."""
        path = item.path if item.path.is_absolute() else self.work_dir / item.path
        path = path.resolve()
        if not path.is_relative_to(self.work_dir.resolve()):
            raise ValueError(f"emitted path is outside work_dir: {item.path}")
        if not path.is_file():
            raise FileNotFoundError(f"emitted file does not exist: {item.path}")
        await self._backend.emit(item.model_copy(update={"path": path}))
        self.emitted += 1

    async def is_known(self, remote_id: str) -> bool:
        """True if any file (in any status) has a source row with this add-on key and id."""
        return await self._backend.is_known(remote_id)

    async def known_remote_ids(self) -> set[str]:
        return await self._backend.known_remote_ids()

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    async def sleep(self, seconds: float) -> bool:
        """Sleep, waking early on cancellation. Returns True if the run was cancelled."""
        try:
            await asyncio.wait_for(self._cancel.wait(), timeout=seconds)
        except TimeoutError:
            return False
        return True

    async def run_subprocess(
        self,
        args: Sequence[str | os.PathLike[str]],
        timeout: float = DEFAULT_SUBPROCESS_TIMEOUT,  # noqa: ASYNC109 - part of the add-on API
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> SubprocessResult:
        """Run a program (argument list, never a shell); see app.crawl.procs."""
        from app.crawl.procs import run_subprocess

        return await run_subprocess(
            args,
            log=self.log,
            cancel_event=self._cancel,
            timeout=timeout,
            cwd=cwd or self.work_dir,
            home=self.state_dir,
            env=env,
        )
