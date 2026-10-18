"""The crawler tool venv at DATA_DIR/tools/venv (yt-dlp and gallery-dl, never in the app venv).

ensure_tools() creates the venv if needed and installs or upgrades the tools to their latest
versions, then writes DATA_DIR/tools/versions.json. The worker runs it before it starts
(app.worker); a failure (for example no network) is logged and the existing tools stay usable.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.storage import DataPaths, get_paths
from app.storage.fs import atomic_write_bytes

logger = logging.getLogger(__name__)

TOOL_PACKAGES = ("yt-dlp[default]", "gallery-dl")
VERSION_DISTRIBUTIONS = ("yt-dlp", "yt-dlp-ejs", "gallery-dl")
INSTALL_TIMEOUT_SECONDS = 900
VERSIONS_SCRIPT = (
    "import importlib.metadata as m, json, sys\n"
    "out = {}\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n"
    "        out[name] = m.version(name)\n"
    "    except m.PackageNotFoundError:\n"
    "        out[name] = None\n"
    "print(json.dumps(out))\n"
)


def venv_bin(paths: DataPaths | None = None) -> Path:
    return (paths or get_paths()).tools_venv / "bin"


def tool_path(name: str, paths: DataPaths | None = None) -> Path:
    """The executable `name` inside the tool venv (it may not exist yet)."""
    if not name or "/" in name or name in {".", ".."}:
        raise ValueError(f"invalid tool name {name!r}")
    return venv_bin(paths) / name


def versions_file(paths: DataPaths | None = None) -> Path:
    return (paths or get_paths()).tools_venv.parent / "versions.json"


def read_versions(paths: DataPaths | None = None) -> dict[str, object]:
    try:
        return json.loads(versions_file(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    logger.info("tools: %s", " ".join(args))
    result = subprocess.run(  # noqa: S603 - fixed argument list, no shell
        args,
        env=env,
        capture_output=True,
        text=True,
        timeout=INSTALL_TIMEOUT_SECONDS,
        check=False,
    )
    for line in (result.stdout + result.stderr).splitlines():
        logger.info("tools | %s", line)
    if result.returncode != 0:
        raise RuntimeError(f"{args[0]} exited with {result.returncode}")
    return result


def install_env(paths: DataPaths) -> dict[str, str]:
    env = dict(os.environ)
    # The app's own project settings must not leak into the tool venv.
    for name in ("UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV", "PYTHONPATH"):
        env.pop(name, None)
    env["UV_CACHE_DIR"] = str(paths.tools_venv.parent / "uv-cache")
    return env


def create_venv(paths: DataPaths, env: dict[str, str]) -> None:
    """Create the tool venv if it does not exist yet."""
    venv = paths.tools_venv
    if (venv / "bin" / "python").exists():
        return
    venv.parent.mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv")
    if uv:
        _run([uv, "venv", "--python", sys.executable, str(venv)], env)
    else:
        _run([sys.executable, "-m", "venv", str(venv)], env)


def pip_install_command(paths: DataPaths) -> list[str]:
    """The installer for the tool venv, up to and including "install" (uv pip or pip)."""
    python = str(paths.tools_venv / "bin" / "python")
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", python]
    return [python, "-m", "pip", "install"]


def install_tools(paths: DataPaths) -> None:
    """Create the venv if missing, then install or upgrade the tool packages."""
    env = install_env(paths)
    create_venv(paths, env)
    _run([*pip_install_command(paths), "--upgrade", *TOOL_PACKAGES], env)


def installed_versions(paths: DataPaths) -> dict[str, str | None]:
    python = paths.tools_venv / "bin" / "python"
    if not python.exists():
        return dict.fromkeys(VERSION_DISTRIBUTIONS)
    result = _run([str(python), "-I", "-c", VERSIONS_SCRIPT, *VERSION_DISTRIBUTIONS], {})
    return json.loads(result.stdout)


def deno_version() -> str | None:
    deno = shutil.which("deno")
    if deno is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed argument list, no shell
            [deno, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    first = result.stdout.strip().splitlines()[:1]
    return first[0].removeprefix("deno ").split(" ")[0] if first else None


def ensure_tools(paths: DataPaths | None = None, *, upgrade: bool = True) -> dict[str, object]:
    """Install or upgrade the tools, write versions.json and return its content.

    Never raises: an install failure is logged and recorded as "error" in versions.json.
    """
    paths = paths or get_paths()
    error = None
    if upgrade or not (paths.tools_venv / "bin" / "python").exists():
        try:
            install_tools(paths)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.error("installing the crawler tools failed: %s", error)
    try:
        versions: dict[str, object] = dict(installed_versions(paths))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        versions = dict.fromkeys(VERSION_DISTRIBUTIONS)
        error = error or f"{type(exc).__name__}: {exc}"
    versions["deno"] = deno_version()
    versions["checked_at"] = datetime.now(UTC).isoformat()
    versions["error"] = error
    atomic_write_bytes(versions_file(paths), json.dumps(versions, indent=2).encode() + b"\n")
    logger.info("crawler tools: %s", json.dumps(versions))
    return versions
