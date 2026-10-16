"""Perceptual hashing hook. Phase 2 fills it in (pHash for images, frame hashes for videos)."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_files import File
from app.storage import DataPaths


async def compute_perceptual(session: AsyncSession, paths: DataPaths, file: File) -> None:
    """Compute and store perceptual hashes for file. Does nothing until phase 2."""
    return None
