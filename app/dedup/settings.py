"""The thresholds used for perceptual matching, read through one function."""

from dataclasses import dataclass

from app.config import Settings, get_settings


@dataclass(frozen=True)
class DedupSettings:
    image_phash_threshold: int
    video_frame_threshold: int


def get_dedup_settings(settings: Settings | None = None) -> DedupSettings:
    """Current thresholds. Values come from configuration; phase 8 lets app_settings override."""
    settings = settings or get_settings()
    return DedupSettings(
        image_phash_threshold=settings.image_phash_threshold,
        video_frame_threshold=settings.video_frame_threshold,
    )
