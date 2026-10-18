"""The thresholds used for perceptual matching, read through one function."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.ops.settings import load_settings


@dataclass(frozen=True)
class DedupSettings:
    image_phash_threshold: int
    video_frame_threshold: int


async def get_dedup_settings(session: AsyncSession) -> DedupSettings:
    """Current thresholds: app_settings values over the environment defaults."""
    settings = await load_settings(session)
    return DedupSettings(
        image_phash_threshold=settings.image_phash_threshold,
        video_frame_threshold=settings.video_frame_threshold,
    )
