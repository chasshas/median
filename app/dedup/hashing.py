"""Perceptual hashes: image pHash and video frame hashes (ARCHITECTURE.md section 8).

Hashes are 64-bit unsigned integers in memory and signed int64 in PostgreSQL bigint columns;
to_signed and to_unsigned convert between the two.
"""

import io
import logging
from pathlib import Path

import imagehash
from PIL import Image, ImageOps

from app.ingest import media_tools

logger = logging.getLogger(__name__)

HASH_SIZE = 8
HASH_BITS = HASH_SIZE * HASH_SIZE
_MASK = (1 << HASH_BITS) - 1
_SIGN = 1 << (HASH_BITS - 1)

# Frame positions for videos, as fractions of the duration; index = frame_index.
VIDEO_FRAME_POSITIONS: tuple[float, ...] = (0.10, 0.30, 0.50, 0.70, 0.90)
# Videos shorter than this hash one frame (frame_index 0) taken at the middle.
SHORT_VIDEO_MS = 1000


def to_signed(value: int) -> int:
    """An unsigned 64-bit hash as the signed int64 stored in a bigint column."""
    if not 0 <= value <= _MASK:
        raise ValueError(f"not an unsigned 64-bit value: {value}")
    return value - (1 << HASH_BITS) if value & _SIGN else value


def to_unsigned(value: int) -> int:
    """A signed int64 read from the database back as the unsigned 64-bit hash."""
    if not -_SIGN <= value < _SIGN:
        raise ValueError(f"not a signed 64-bit value: {value}")
    return value & _MASK


def hamming(a: int, b: int) -> int:
    """Number of differing bits between two unsigned 64-bit hashes."""
    return (a ^ b).bit_count()


def hash_to_int(h: imagehash.ImageHash) -> int:
    """The hash bits, row-major and most significant bit first (same as int(str(h), 16))."""
    value = 0
    for bit in h.hash.flatten():
        value = (value << 1) | int(bool(bit))
    return value


def phash_image(img: Image.Image) -> int:
    """pHash (hash_size 8) of the EXIF-transposed RGB first frame, as an unsigned int."""
    img.seek(0)
    frame = ImageOps.exif_transpose(img).convert("RGB")
    return hash_to_int(imagehash.phash(frame, hash_size=HASH_SIZE))


def phash_image_file(path: Path) -> int:
    with Image.open(path) as img:
        return phash_image(img)


def phash_png_bytes(data: bytes) -> int:
    with Image.open(io.BytesIO(data)) as img:
        return phash_image(img)


def video_frame_times(duration_ms: int) -> list[tuple[int, float]]:
    """(frame_index, seconds) pairs to hash for a video of the given duration."""
    seconds = duration_ms / 1000
    if duration_ms < SHORT_VIDEO_MS:
        return [(0, seconds / 2)]
    return [(index, seconds * pos) for index, pos in enumerate(VIDEO_FRAME_POSITIONS)]


async def video_frame_hashes(path: Path, duration_ms: int) -> dict[int, int]:
    """Unsigned pHash per frame_index. Frames ffmpeg cannot produce are left out."""
    hashes: dict[int, int] = {}
    times = video_frame_times(duration_ms)
    for index, at in times:
        data = await _frame(path, at)
        if not data and len(times) == 1:
            data = await _frame(path, None)  # very short clip: fall back to the first frame
        if data:
            hashes[index] = phash_png_bytes(data)
    return hashes


async def _frame(path: Path, at: float | None) -> bytes:
    try:
        return await media_tools.ffmpeg_frame_png(path, at_seconds=at)
    except media_tools.ToolError as exc:
        logger.warning("no frame at %s s of %s: %s", at, path, exc)
        return b""
