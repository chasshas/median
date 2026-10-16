"""Generated test media (no binary fixtures)."""

import io
import subprocess
from pathlib import Path

from PIL import Image


def png_bytes(
    color: tuple[int, int, int] = (200, 30, 30), size: tuple[int, int] = (64, 48)
) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


def jpeg_bytes(
    color: tuple[int, int, int] = (30, 120, 200),
    size: tuple[int, int] = (800, 400),
    orientation: int | None = None,
) -> bytes:
    img = Image.new("RGB", size, color)
    out = io.BytesIO()
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        img.save(out, "JPEG", exif=exif.tobytes())
    else:
        img.save(out, "JPEG")
    return out.getvalue()


def gif_animated_bytes() -> bytes:
    frames = [Image.new("RGB", (40, 30), (i * 100, 50, 200 - i * 60)) for i in range(3)]
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return out.getvalue()


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def make_video(path: Path, seconds: float = 2.0, size: str = "320x240", rate: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate={rate}",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
            str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


def make_audio(path: Path, seconds: float = 1.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


def make_audio_with_cover(path: Path, cover_png: Path) -> Path:
    tone = make_audio(path.with_name("plain-" + path.name))
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-i", str(tone), "-i", str(cover_png),
            "-map", "0", "-map", "1", "-c:a", "copy", "-c:v", "png",
            "-id3v2_version", "3", "-disposition:v", "attached_pic",
            str(path),
        ],
        check=True,
    )  # fmt: skip
    return path
