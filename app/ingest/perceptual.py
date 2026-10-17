"""Perceptual hashing hook called by process_ingest; the work lives in app.dedup."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_files import File
from app.dedup import service
from app.storage import DataPaths


async def compute_perceptual(session: AsyncSession, paths: DataPaths, file: File) -> None:
    """Compute perceptual hashes for file and record files similar to it (any status)."""
    await service.compute_perceptual(session, paths, file)
