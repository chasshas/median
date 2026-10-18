"""update_tools: upgrade yt-dlp[default] and gallery-dl in the tool venv (DATA_DIR/tools/venv).

The versions before and after go to DATA_DIR/tools/versions.json and the job stats. Deno is
part of the image and changes only with an image rebuild.
"""

import asyncio
import json
import logging
import subprocess
from datetime import UTC, datetime
from typing import Any

from app.crawl.tools import (
    INSTALL_TIMEOUT_SECONDS,
    TOOL_PACKAGES,
    create_venv,
    deno_version,
    install_env,
    installed_versions,
    pip_install_command,
    read_versions,
    versions_file,
)
from app.ops.runs import JobFailed
from app.storage import DataPaths
from app.storage.fs import atomic_write_bytes


def _pip(args: list[str], env: dict[str, str], log: logging.Logger) -> None:
    log.info("running %s", " ".join(args))
    result = subprocess.run(  # noqa: S603 - argument list, no shell
        args,
        env=env,
        capture_output=True,
        text=True,
        timeout=INSTALL_TIMEOUT_SECONDS,
        check=False,
    )
    for line in (result.stdout + result.stderr).splitlines():
        log.info("pip | %s", line)
    if result.returncode != 0:
        raise RuntimeError(f"{args[0]} exited with {result.returncode}")


def _versions(paths: DataPaths) -> dict[str, str | None]:
    try:
        return dict(installed_versions(paths))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        return {}


def _update(paths: DataPaths, log: logging.Logger, pip: list[str] | None) -> dict[str, Any]:
    env = install_env(paths)
    before = _versions(paths)
    error = None
    try:
        create_venv(paths, env)
        _pip([*(pip or pip_install_command(paths)), "--upgrade", *TOOL_PACKAGES], env, log)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        error = f"{type(exc).__name__}: {exc}"
    after = _versions(paths)
    now = datetime.now(UTC).isoformat()
    changed = {
        name: [before.get(name), version]
        for name, version in after.items()
        if before.get(name) != version
    }
    record: dict[str, Any] = {
        **after,
        "deno": deno_version(),
        "checked_at": now,
        "updated_at": now,
        "previous": before,
        "error": error,
    }
    atomic_write_bytes(versions_file(paths), json.dumps(record, indent=2).encode() + b"\n")
    stats = {"before": before, "after": after, "changed": changed, "deno": record["deno"]}
    for name, (old, new) in changed.items():
        log.info("%s: %s -> %s", name, old, new)
    if not changed:
        log.info("no version changed: %s", after)
    if error:
        raise JobFailed(f"updating the crawler tools failed: {error}", stats)
    return stats


async def update_tools(
    paths: DataPaths, log: logging.Logger, pip: list[str] | None = None
) -> dict[str, Any]:
    """Upgrade the tools. pip replaces the installer command (up to "install") in tests."""
    return await asyncio.to_thread(_update, paths, log, pip)


def current_versions(paths: DataPaths) -> dict[str, Any]:
    return read_versions(paths)
