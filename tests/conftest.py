"""Test setup: the separate test database and a temporary DATA_DIR.

The environment is pointed at the test database before any app module is imported,
so code that reads settings directly can never reach the app database or real data.
"""

import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://median:median@localhost:5432/median_test"
)
_APP_DATABASE_URL = os.environ.get("DATABASE_URL")


def _db_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


if _APP_DATABASE_URL and _db_name(_APP_DATABASE_URL) == _db_name(TEST_DATABASE_URL):
    raise RuntimeError("TEST_DATABASE_URL must name a different database than DATABASE_URL")

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="median-test-data-"))
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["DATA_DIR"] = str(_TEST_DATA_DIR)

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402
from app.db.migrate import migrate  # noqa: E402
from app.main import create_app  # noqa: E402

get_settings.cache_clear()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if "network" in (config.getoption("-m") or ""):
        return
    skip = pytest.mark.skip(reason="network test; run with -m network")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


def reset_database(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


@pytest.fixture(scope="session")
def data_dir() -> Iterator[Path]:
    yield _TEST_DATA_DIR
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)


@pytest.fixture(scope="session")
def migrated_db() -> str:
    """Rebuild the test database from scratch with all migrations, once per session."""
    reset_database(TEST_DATABASE_URL)
    migrate(TEST_DATABASE_URL)
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def settings(migrated_db: str, data_dir: Path) -> Settings:
    return Settings(database_url=migrated_db, test_database_url=migrated_db, data_dir=data_dir)


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
def db_conn(migrated_db: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        yield conn


@pytest.fixture
def paths(data_dir: Path):
    from app.storage import DataPaths

    return DataPaths(data_dir)


@pytest.fixture
def clean_ingest(migrated_db: str, data_dir: Path) -> Iterator[None]:
    """Empty the ingest tables, the job queue and the media directories before a test."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE file_tags, tags, ingest_events, file_sources, files, procrastinate_jobs"
            " RESTART IDENTITY CASCADE"
        )
    for name in ("staging", "archive", "thumbs", "trash"):
        shutil.rmtree(data_dir / name, ignore_errors=True)
    yield


@pytest.fixture
async def sessionmaker(migrated_db: str):
    from app.db.engine import job_sessionmaker

    async with job_sessionmaker(migrated_db) as maker:
        yield maker
