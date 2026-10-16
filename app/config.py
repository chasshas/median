from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, read from environment variables (see .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # libpq-style URLs (postgresql://user:pass@host:port/db).
    database_url: str = "postgresql://median:median@localhost:5432/median"
    test_database_url: str = "postgresql://median:median@localhost:5432/median_test"
    data_dir: Path = Path("/data")
    app_timezone: str = "Asia/Seoul"
    app_password: str = ""
    image_phash_threshold: int = Field(default=6, ge=0, le=64)
    video_frame_threshold: int = Field(default=8, ge=0, le=64)
    trash_retention_days: int = Field(default=7, ge=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def sqlalchemy_url(url: str) -> str:
    """Turn a libpq URL into the SQLAlchemy form that selects the psycopg 3 driver."""
    for prefix in ("postgresql+psycopg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    raise ValueError(f"unsupported database URL scheme: {url.split(':', 1)[0]}")


def libpq_url(url: str) -> str:
    """Turn a SQLAlchemy psycopg URL back into a libpq URL (used by Procrastinate)."""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)
