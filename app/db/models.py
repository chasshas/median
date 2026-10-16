"""Imports every model module so Base.metadata is complete (used by Alembic).

Each phase adds its model modules here (for example app.db.models_files).
"""

from app.db.base import Base

__all__ = ["Base"]
