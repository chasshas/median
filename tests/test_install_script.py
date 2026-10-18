"""install.sh --configure-only: the .env it writes (the full install runs in CI)."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    target = tmp_path / "median"
    target.mkdir()
    for name in ("install.sh", ".env.example", "docker-compose.yml"):
        shutil.copy(ROOT / name, target / name)
    (target / "app").mkdir()
    return target


def run_install(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(checkout / "install.sh"), "--configure-only", "--yes", *args],
        cwd=checkout,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_writes_env_with_secrets_and_deployment_mode(checkout: Path) -> None:
    result = run_install(checkout, "--timezone", "Europe/Berlin", "--port", "8123")
    assert result.returncode == 0, result.stderr
    env_path = checkout / ".env"
    assert env_path.stat().st_mode & 0o777 == 0o600
    env = read_env(env_path)
    db_password = env["POSTGRES_PASSWORD"]
    assert len(db_password) == 32 and db_password != "median"
    assert env["DATABASE_URL"] == f"postgresql://median:{db_password}@db:5432/median"
    assert env["TEST_DATABASE_URL"] == f"postgresql://median:{db_password}@db:5432/median_test"
    assert len(env["SECRET_KEY"]) == 64
    assert env["APP_PASSWORD"].startswith("'") and len(env["APP_PASSWORD"]) == 18
    assert env["APP_TIMEZONE"] == "Europe/Berlin"
    assert env["WEB_PORT"] == "8123"
    assert env["COMPOSE_FILE"] == "docker-compose.yml"
    # Every other setting of the template is kept.
    template = read_env(ROOT / ".env.example")
    assert set(template) <= set(env)
    assert env["TOOLS_UPDATE_SCHEDULE"] == template["TOOLS_UPDATE_SCHEDULE"]


def test_password_is_stored_literally(checkout: Path) -> None:
    result = run_install(checkout, "--password", "a$b c#d\\e")
    assert result.returncode == 0, result.stderr
    assert read_env(checkout / ".env")["APP_PASSWORD"] == "'a$b c#d\\e'"


def test_no_password_and_existing_env_kept(checkout: Path) -> None:
    assert run_install(checkout, "--no-password").returncode == 0
    first = (checkout / ".env").read_text()
    assert read_env(checkout / ".env")["APP_PASSWORD"] == "''"
    again = run_install(checkout, "--password", "other")
    assert again.returncode == 0, again.stderr
    assert "Keeping the existing .env" in again.stdout
    assert (checkout / ".env").read_text() == first


@pytest.mark.parametrize(
    "args",
    [("--password", "it's"), ("--port", "http"), ("--port", "70000"), ("--timezone", "a b")],
)
def test_bad_values_are_refused(checkout: Path, args: tuple[str, str]) -> None:
    result = run_install(checkout, *args)
    assert result.returncode != 0
    assert "error:" in result.stderr
    assert not (checkout / ".env").exists()
