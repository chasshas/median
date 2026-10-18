import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import worker
from app.crawl import tools
from app.storage import DataPaths


@pytest.fixture
def data(tmp_path: Path) -> DataPaths:
    return DataPaths(tmp_path / "data")


def test_tool_path(data: DataPaths) -> None:
    assert tools.tool_path("yt-dlp", data) == data.root / "tools" / "venv" / "bin" / "yt-dlp"
    assert tools.tool_path("gallery-dl", data).parent == data.tools_venv / "bin"
    for bad in ("", "../x", "a/b", ".."):
        with pytest.raises(ValueError):
            tools.tool_path(bad, data)


def test_install_with_uv_creates_venv_then_upgrades(data: DataPaths, monkeypatch) -> None:
    calls: list[list[str]] = []
    envs: list[dict[str, str]] = []

    def fake_run(args: list[str], env: dict[str, str]):
        calls.append(args)
        envs.append(env)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tools, "_run", fake_run)
    monkeypatch.setattr(tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/opt/venv")
    tools.install_tools(data)
    venv = str(data.tools_venv)
    assert calls == [
        ["/usr/bin/uv", "venv", "--python", sys.executable, venv],
        [
            "/usr/bin/uv",
            "pip",
            "install",
            "--python",
            f"{venv}/bin/python",
            "--upgrade",
            "yt-dlp[default]",
            "gallery-dl",
        ],
    ]
    assert "UV_PROJECT_ENVIRONMENT" not in envs[0]
    assert envs[0]["UV_CACHE_DIR"] == str(data.root / "tools" / "uv-cache")

    # The venv exists: only the upgrade runs.
    (data.tools_venv / "bin").mkdir(parents=True)
    (data.tools_venv / "bin" / "python").touch()
    calls.clear()
    tools.install_tools(data)
    assert [c[1] for c in calls] == ["pip"]


def test_ensure_tools_writes_versions(data: DataPaths, monkeypatch) -> None:
    def fake_install(paths: DataPaths) -> None:
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(paths.tools_venv)], check=True
        )

    monkeypatch.setattr(tools, "install_tools", fake_install)
    monkeypatch.setattr(tools, "deno_version", lambda: "2.9.7")
    versions = tools.ensure_tools(data)
    on_disk = json.loads((data.root / "tools" / "versions.json").read_text())
    assert on_disk == versions
    assert versions["yt-dlp"] is None and versions["gallery-dl"] is None  # empty test venv
    assert versions["deno"] == "2.9.7"
    assert versions["error"] is None
    assert tools.read_versions(data) == versions


def test_ensure_tools_never_raises(data: DataPaths, monkeypatch) -> None:
    def broken(paths: DataPaths) -> None:
        raise RuntimeError("uv exited with 2")

    monkeypatch.setattr(tools, "install_tools", broken)
    versions = tools.ensure_tools(data)
    assert versions["error"] == "RuntimeError: uv exited with 2"
    assert versions["yt-dlp"] is None
    assert json.loads((data.root / "tools" / "versions.json").read_text())["error"]


def test_worker_installs_tools_then_execs_procrastinate(monkeypatch) -> None:
    seen: list[object] = []
    monkeypatch.delenv("MEDIAN_SKIP_TOOLS", raising=False)

    async def recover() -> dict[str, list[int]]:
        seen.append("recover")
        return {}

    monkeypatch.setattr(worker, "recover", recover)
    monkeypatch.setattr(worker, "ensure_tools", lambda: seen.append("tools"))
    monkeypatch.setattr(worker.os, "execvp", lambda prog, args: seen.append((prog, args)))
    worker.main(["--concurrency=4"])
    assert seen == [
        "recover",
        "tools",
        (
            "procrastinate",
            [
                "procrastinate",
                "--app=app.jobs.app.job_app",
                "--log-level=info",
                "worker",
                "--concurrency=4",
            ],
        ),
    ]
