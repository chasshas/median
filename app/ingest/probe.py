"""Detect type, dimensions and duration: Pillow first, then ffprobe, then the extension."""

import asyncio
import mimetypes
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from app.db.models_files import MediaKind
from app.ingest import media_tools

IMAGE_EXTS = {
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "avif", "heic", "heif", "jfif",
}  # fmt: skip
VIDEO_EXTS = {
    "mp4", "m4v", "mov", "mkv", "webm", "avi", "wmv", "flv", "ts", "mpg", "mpeg", "3gp", "ogv",
}  # fmt: skip
AUDIO_EXTS = {"mp3", "m4a", "aac", "flac", "ogg", "oga", "opus", "wav", "wma", "mka"}

# Pillow format -> extension.
PILLOW_EXT = {
    "JPEG": "jpg",
    "PNG": "png",
    "GIF": "gif",
    "WEBP": "webp",
    "BMP": "bmp",
    "TIFF": "tiff",
    "MPO": "jpg",
    "ICO": "ico",
    "AVIF": "avif",
}

# ffprobe format_name -> extensions it may carry (first = default).
FFPROBE_EXTS = {
    "mov,mp4,m4a,3gp,3g2,mj2": ["mp4", "m4v", "mov", "m4a", "3gp", "3g2"],
    "matroska,webm": ["mkv", "webm", "mka"],
    "avi": ["avi"],
    "asf": ["wmv", "wma", "asf"],
    "flv": ["flv"],
    "mpegts": ["ts"],
    "mpeg": ["mpg", "mpeg"],
    "mp3": ["mp3"],
    "flac": ["flac"],
    "ogg": ["ogg", "ogv", "oga", "opus"],
    "wav": ["wav"],
    "aac": ["aac"],
}
EXTRA_MIME = {
    "webp": "image/webp",
    "mkv": "video/x-matroska",
    "mka": "audio/x-matroska",
    "webm": "video/webm",
    "m4a": "audio/mp4",
    "flac": "audio/flac",
    "opus": "audio/ogg",
    "avif": "image/avif",
}


@dataclass
class ProbeResult:
    media_kind: MediaKind
    mime: str
    ext: str | None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def original_ext(filename: str | None) -> str | None:
    if not filename or "." not in filename:
        return None
    ext = filename.rsplit(".", 1)[1].lower()
    return ext if ext.isalnum() and len(ext) <= 10 else None


def mime_for(ext: str | None, default: str = "application/octet-stream") -> str:
    if not ext:
        return default
    return EXTRA_MIME.get(ext) or mimetypes.guess_type(f"x.{ext}")[0] or default


def probe_image(path: Path) -> ProbeResult | None:
    """Pillow: format and size, decoding the first frame. None if Pillow cannot decode it."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(path) as img:
                fmt = img.format or ""
                width, height = img.size
                frames = getattr(img, "n_frames", 1)
                img.load()
    except Exception:
        return None
    ext = PILLOW_EXT.get(fmt, fmt.lower() or None)
    mime = Image.MIME.get(fmt) or mime_for(ext)
    metadata: dict[str, Any] = {"format": fmt}
    if frames > 1:
        metadata["animated"] = True
        metadata["frames"] = frames
    return ProbeResult(MediaKind.image, mime, ext, width, height, None, metadata)


def _is_image_container(fmt: dict[str, Any]) -> bool:
    name = str(fmt.get("format_name", ""))
    return name == "image2" or name.endswith("_pipe")


def _pick_ext(format_name: str, orig: str | None, kind: MediaKind) -> str | None:
    candidates = FFPROBE_EXTS.get(format_name)
    if not candidates:
        return orig or format_name.split(",")[0] or None
    if orig in candidates:
        return orig
    if kind is MediaKind.audio:
        audio = [c for c in candidates if c in AUDIO_EXTS]
        if audio:
            return audio[0]
    return candidates[0]


def interpret_ffprobe(info: dict[str, Any], orig: str | None) -> ProbeResult | None:
    fmt = info.get("format") or {}
    streams = info.get("streams") or []
    if not fmt or _is_image_container(fmt):
        return None
    video = [
        s
        for s in streams
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
    ]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if video:
        kind = MediaKind.video
    elif audio:
        kind = MediaKind.audio
    else:
        return None
    duration_ms = None
    try:
        duration = float(fmt.get("duration") or (video or audio)[0].get("duration") or 0)
        duration_ms = round(duration * 1000) or None
    except (TypeError, ValueError):
        pass
    format_name = str(fmt.get("format_name", ""))
    ext = _pick_ext(format_name, orig, kind)
    metadata: dict[str, Any] = {"format": format_name}
    width = height = None
    if video:
        width, height = video[0].get("width"), video[0].get("height")
        metadata["video_codec"] = video[0].get("codec_name")
    if audio:
        metadata["audio_codec"] = audio[0].get("codec_name")
    if any((s.get("disposition") or {}).get("attached_pic") for s in streams):
        metadata["cover_art"] = True
    if fmt.get("bit_rate"):
        metadata["bit_rate"] = int(fmt["bit_rate"])
    default_mime = "video/mp4" if kind is MediaKind.video else "audio/mpeg"
    return ProbeResult(kind, mime_for(ext, default_mime), ext, width, height, duration_ms, metadata)


async def probe(path: Path, original_filename: str | None) -> ProbeResult:
    """Classify a file. Undecodable files become media_kind other; if their extension promised
    an image, video or audio file, metadata.probe_error says why."""
    orig = original_ext(original_filename)
    result = await asyncio.to_thread(probe_image, path)
    if result is not None:
        return result
    ffprobe_error: str | None = None
    try:
        info = await media_tools.ffprobe(path)
    except (media_tools.ToolError, ValueError) as exc:
        info, ffprobe_error = {}, str(exc)
    result = interpret_ffprobe(info, orig)
    if result is not None:
        return result
    metadata: dict[str, Any] = {}
    if orig in IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS:
        expected = "image" if orig in IMAGE_EXTS else "video" if orig in VIDEO_EXTS else "audio"
        detail = f"extension .{orig} suggests {expected}, but the file could not be decoded"
        if ffprobe_error:
            detail += f" ({ffprobe_error})"
        metadata["probe_error"] = detail
    return ProbeResult(MediaKind.other, mime_for(orig), orig, metadata=metadata)
