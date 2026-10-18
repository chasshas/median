import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from app.crawl.tools import versions_file
from app.jobs import run_queued_jobs
from app.ops import tools_update
from app.ops.runs import JobFailed

pytestmark = pytest.mark.usefixtures("clean_ingest")
log = logging.getLogger("test.tools")

# A stand-in for "pip install --upgrade ...": it records its arguments and "installs" newer
# versions by writing dist-info metadata into the venv's site-packages.
FAKE_PIP = """#!{python}
import pathlib, sys
site = pathlib.Path({site!r})
pathlib.Path({calls!r}).write_text(" ".join(sys.argv[1:]))
if {fail!r}:
    print("ERROR: no network", file=sys.stderr)
    sys.exit(1)
for name, version in [("yt_dlp", "2026.10.1"), ("yt_dlp_ejs", "0.9.0"), ("gallery_dl", "1.33.0")]:
    for old in site.glob(name + "-*.dist-info"):
        for f in old.iterdir():
            f.unlink()
        old.rmdir()
    info = site / f"{{name}}-{{version}}.dist-info"
    info.mkdir()
    meta = f"Metadata-Version: 2.1\\nName: {{name.replace('_', '-')}}\\nVersion: {{version}}\\n"
    (info / "METADATA").write_text(meta)
print("Successfully installed yt-dlp gallery-dl")
"""


def make_venv(paths) -> Path:
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(paths.tools_venv)], check=True
    )
    site = next((paths.tools_venv / "lib").glob("python*/site-packages"))
    info = site / "yt_dlp-2025.1.1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: yt-dlp\nVersion: 2025.1.1\n")
    return site


def fake_pip(tmp_path: Path, site: Path, fail: bool = False) -> tuple[list[str], Path]:
    calls = tmp_path / "calls.txt"
    script = tmp_path / "pip"
    script.write_text(
        FAKE_PIP.format(python=sys.executable, site=str(site), calls=str(calls), fail=fail)
    )
    script.chmod(0o755)
    return [str(script), "install"], calls


@pytest.fixture
def tool_paths(paths):
    import shutil

    shutil.rmtree(paths.root / "tools", ignore_errors=True)
    yield paths
    shutil.rmtree(paths.root / "tools", ignore_errors=True)


async def test_update_records_versions_before_and_after(tool_paths, tmp_path) -> None:
    site = make_venv(tool_paths)
    pip, calls = fake_pip(tmp_path, site)
    stats = await tools_update.update_tools(tool_paths, log, pip=pip)
    assert calls.read_text() == "install --upgrade yt-dlp[default] gallery-dl"
    assert stats["before"] == {"yt-dlp": "2025.1.1", "yt-dlp-ejs": None, "gallery-dl": None}
    assert stats["after"] == {"yt-dlp": "2026.10.1", "yt-dlp-ejs": "0.9.0", "gallery-dl": "1.33.0"}
    assert stats["changed"]["yt-dlp"] == ["2025.1.1", "2026.10.1"]
    saved = json.loads(versions_file(tool_paths).read_text())
    assert saved["yt-dlp"] == "2026.10.1" and saved["gallery-dl"] == "1.33.0"
    assert saved["previous"]["yt-dlp"] == "2025.1.1"
    assert saved["error"] is None and saved["updated_at"]

    again = await tools_update.update_tools(tool_paths, log, pip=pip)
    assert again["changed"] == {}


async def test_failed_update_keeps_the_error(tool_paths, tmp_path) -> None:
    site = make_venv(tool_paths)
    pip, _ = fake_pip(tmp_path, site, fail=True)
    with pytest.raises(JobFailed) as err:
        await tools_update.update_tools(tool_paths, log, pip=pip)
    assert "exited with 1" in str(err.value)
    assert err.value.stats["after"]["yt-dlp"] == "2025.1.1"
    assert "exited with 1" in json.loads(versions_file(tool_paths).read_text())["error"]


async def test_update_job_from_settings_shows_versions(
    client, tool_paths, tmp_path, db_conn, monkeypatch
) -> None:
    site = make_venv(tool_paths)
    pip, _ = fake_pip(tmp_path, site)
    monkeypatch.setattr(tools_update, "pip_install_command", lambda paths: pip)
    r = await client.post("/settings/run/update_tools")
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    await run_queued_jobs()
    status, stats = db_conn.execute(
        "SELECT status::text, stats FROM job_runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert status == "succeeded" and stats["after"]["gallery-dl"] == "1.33.0"
    page = await client.get("/settings")
    assert 'data-version="yt-dlp">2026.10.1<' in page.text
    assert 'data-version="gallery-dl">1.33.0<' in page.text
    assert "yt-dlp 2025.1.1 &rarr; 2026.10.1" in page.text
    job = await client.get(f"/jobs/{run_id}")
    assert "1.33.0" in job.text
