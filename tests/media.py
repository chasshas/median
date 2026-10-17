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


# --- structured media for perceptual hashing (flat images all share one pHash) -------------


def structured_image(seed: int, size: tuple[int, int] = (640, 480)) -> Image.Image:
    """A deterministic image with real structure: gradient, random shapes and text."""
    import random

    from PIL import ImageDraw, ImageFont

    rng = random.Random(seed)
    w, h = size

    def color() -> tuple[int, int, int]:
        return (rng.randrange(256), rng.randrange(256), rng.randrange(256))

    start, end = color(), color()
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    horizontal = rng.random() < 0.5
    steps = w if horizontal else h
    for i in range(steps):
        t = i / max(steps - 1, 1)
        c = tuple(int(a + (b - a) * t) for a, b in zip(start, end, strict=True))
        if horizontal:
            draw.line([(i, 0), (i, h)], fill=c)
        else:
            draw.line([(0, i), (w, i)], fill=c)
    for _ in range(14):
        x0, y0 = rng.randrange(w), rng.randrange(h)
        x1, y1 = rng.randrange(x0, w + 1), rng.randrange(y0, h + 1)
        kind = rng.randrange(4)
        if kind == 0:
            draw.ellipse([x0, y0, x1, y1], fill=color())
        elif kind == 1:
            draw.rectangle([x0, y0, x1, y1], fill=color())
        elif kind == 2:
            points = [(rng.randrange(w), rng.randrange(h)) for _ in range(rng.randrange(3, 7))]
            draw.polygon(points, fill=color())
        else:
            draw.line([(x0, y0), (x1, y1)], fill=color(), width=rng.randrange(3, 20))
    font = ImageFont.load_default(size=max(24, h // 8))
    for _ in range(2):
        word = "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(5))
        draw.text((rng.randrange(w // 2), rng.randrange(h - h // 6)), word, fill=color(), font=font)
    return img


def encode(img: Image.Image, fmt: str, **options: object) -> bytes:
    out = io.BytesIO()
    img.save(out, fmt, **options)
    return out.getvalue()


def structured_png(seed: int, size: tuple[int, int] = (640, 480)) -> bytes:
    return encode(structured_image(seed, size), "PNG")


def variant_resized(data: bytes, factor: float = 0.5) -> bytes:
    with Image.open(io.BytesIO(data)) as img:
        size = (max(1, int(img.width * factor)), max(1, int(img.height * factor)))
        return encode(img.convert("RGB").resize(size, Image.Resampling.LANCZOS), "PNG")


def variant_jpeg(data: bytes, quality: int = 60) -> bytes:
    with Image.open(io.BytesIO(data)) as img:
        return encode(img.convert("RGB"), "JPEG", quality=quality)


def variant_webp(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as img:
        return encode(img.convert("RGB"), "WEBP", quality=90)


def make_lavfi_video(
    path: Path, source: str = "testsrc2", seconds: float = 4.0, size: str = "320x240"
) -> Path:
    """A video from an ffmpeg lavfi source (testsrc2, mandelbrot, ...)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"{source}=size={size}:rate=10",
            "-t", f"{seconds}",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
            str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


def reencode_video(src: Path, dest: Path, size: str = "160x120", bitrate: str = "120k") -> Path:
    """The same video at another resolution and bitrate."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src),
            "-s", size, "-b:v", bitrate, "-pix_fmt", "yuv420p", "-c:v", "libx264",
            "-preset", "ultrafast", str(dest),
        ],
        check=True,
    )  # fmt: skip
    return dest
