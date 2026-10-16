"""Apply Alembic migrations and the Procrastinate schema: python -m app.db.migrate."""

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.config import get_settings, sqlalchemy_url
from app.jobs.schema import ensure_job_schema

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def alembic_config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", sqlalchemy_url(database_url))
    return config


def migrate(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")
    applied = ensure_job_schema(database_url)
    print("procrastinate schema:", "applied" if applied else "already present")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", help="defaults to DATABASE_URL")
    args = parser.parse_args()
    migrate(args.database_url or get_settings().database_url)


if __name__ == "__main__":
    main()
