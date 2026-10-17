"""Imports every model module so Base.metadata is complete (used by Alembic)."""

from app.db import models_crawl, models_dedup, models_files, models_library, models_review
from app.db.base import Base

__all__ = [
    "Base",
    "models_crawl",
    "models_dedup",
    "models_files",
    "models_library",
    "models_review",
]
