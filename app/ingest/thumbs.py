"""WebP thumbnails, max edge 320 px, quality 80."""

import asyncio
import io
import logging
from pathlib import Path

from PIL import Image, ImageOps

from app.db.models_files import MediaKind
from app.ingest import media_tools
from app.storage import atomic_write_bytes

logger = logging.getLogger(__name__)

THUMB_MAX_EDGE = 320
THUMB_QUALITY = 80


def render_thumb(img: Image.Image) -> bytes:
    """EXIF-transposed first frame, scaled to fit 320x320, as WebP bytes."""
    img.seek(0)
    frame = ImageOps.exif_transpose(img)
    has_alpha = frame.mode in ("RGBA", "LA", "PA") or (
        frame.mode == "P" and "transparency" in frame.info
    )
    frame = frame.convert("RGBA" if has_alpha else "RGB")
    frame.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    frame.save(out, "WEBP", quality=THUMB_QUALITY)
    return out.getvalue()


def thumb_from_image_file(path: Path) -> bytes:
    with Image.open(path) as img:
        return render_thumb(img)


def thumb_from_png_bytes(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as img:
        return render_thumb(img)


async def make_thumbnail(
    path: Path,
    media_kind: MediaKind,
    duration_ms: int | None,
    dest: Path,
    *,
    has_cover: bool = False,
) -> bool:
    """Write the thumbnail for path to dest. Returns False when the file gets none."""
    data: bytes | None = None
    if media_kind is MediaKind.image:
        data = await asyncio.to_thread(thumb_from_image_file, path)
    elif media_kind is MediaKind.video:
        at = duration_ms / 10000 if duration_ms else 1.0
        png = await _frame(path, at)
        if png is None and at:
            png = await _frame(path, None)  # very short clips: take the first frame
        if png is not None:
            data = await asyncio.to_thread(thumb_from_png_bytes, png)
    elif media_kind is MediaKind.audio and has_cover:
        png = await _frame(path, None)  # embedded cover art
        if png is not None:
            data = await asyncio.to_thread(thumb_from_png_bytes, png)
    if data is None:
        return False
    await asyncio.to_thread(atomic_write_bytes, dest, data)
    return True


async def _frame(path: Path, at: float | None) -> bytes | None:
    try:
        png = await media_tools.ffmpeg_frame_png(path, at_seconds=at)
    except media_tools.ToolError as exc:
        logger.info("no frame from %s at %s: %s", path.name, at, exc)
        return None
    return png or None
