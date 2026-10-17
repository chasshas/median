"""Generate unique test media for scripts/smoke.sh: python scripts/smoke_media.py <dir>.

<dir>/upload: 3 PNG images and 1 MP4 video.
<dir>/import: 2 images, a video, an MP3, a text file and a corrupt JPEG (one in a subdir).
<dir>/similar: original.png (random shapes, gradient and text), half.png (resized to 50 percent)
and reencoded.jpg (JPEG quality 60) for the perceptual duplicate checks.
<dir>/review: 20 structured PNG images for the review queue checks.
<dir>/review_pair: original.png, resized.png (50 percent) and variant.jpg (JPEG quality 60).
<dir>/library: crawled.png, ingested with crawler tags for the phase 4 checks.
<dir>/search: 6 structured PNG images and 1 MP4 video for the phase 7 search checks.
Every run produces new content (random pixels and metadata), so repeated runs stay "new".
"""

import os
import random
import subprocess
import sys
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def image(path: Path) -> None:
    Image.frombytes("RGB", (96, 64), os.urandom(96 * 64 * 3)).save(path)


def structured(rng: random.Random, size: tuple[int, int] = (640, 480)) -> Image.Image:
    """Shapes over a gradient with text: an image whose pHash means something."""
    w, h = size

    def color() -> tuple[int, int, int]:
        return (rng.randrange(256), rng.randrange(256), rng.randrange(256))

    start, end = color(), color()
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        fill = tuple(int(a + (b - a) * t) for a, b in zip(start, end, strict=True))
        draw.line([(0, y), (w, y)], fill=fill)
    for _ in range(14):
        x0, y0 = rng.randrange(w), rng.randrange(h)
        box = [x0, y0, rng.randrange(x0, w + 1), rng.randrange(y0, h + 1)]
        [draw.ellipse, draw.rectangle][rng.randrange(2)](box, fill=color())
    font = ImageFont.load_default(size=60)
    draw.text((rng.randrange(w // 2), rng.randrange(h - 80)), uuid.uuid4().hex[:5], font=font)
    return img


def similar_set(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    img = structured(random.Random(os.urandom(16)))
    img.save(directory / "original.png")
    img.resize((img.width // 2, img.height // 2), Image.Resampling.LANCZOS).save(
        directory / "half.png"
    )
    img.save(directory / "reencoded.jpg", quality=60)


def review_set(directory: Path, count: int = 20, prefix: str = "review") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        structured(random.Random(os.urandom(16))).save(directory / f"{prefix}{i:02d}.png")


def review_pair(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    img = structured(random.Random(os.urandom(16)))
    img.save(directory / "original.png")
    img.resize((img.width // 2, img.height // 2), Image.Resampling.LANCZOS).save(
        directory / "resized.png"
    )
    img.save(directory / "variant.jpg", quality=60)


def video(path: Path, seconds: float = 4.0) -> None:
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x240:rate=15",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
            "-metadata", f"title={uuid.uuid4()}", "-movflags", "+faststart",
            str(path),
        ],
        check=True,
    )  # fmt: skip


def audio(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-metadata", f"title={uuid.uuid4()}",
            str(path),
        ],
        check=True,
    )  # fmt: skip


def main() -> None:
    root = Path(sys.argv[1])
    upload, imports = root / "upload", root / "import"
    (imports / "sub").mkdir(parents=True, exist_ok=True)
    upload.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        image(upload / f"image{i}.png")
    video(upload / "video.mp4")
    image(imports / "photo.png")
    image(imports / "sub" / "photo2.jpg")
    video(imports / "sub" / "clip.mp4", seconds=2.0)
    audio(imports / "tone.mp3")
    (imports / "notes.txt").write_text(f"notes {uuid.uuid4()}\n")
    (imports / "broken.jpg").write_bytes(b"\xff\xd8\xff" + os.urandom(64))
    similar_set(root / "similar")
    review_set(root / "review")
    review_pair(root / "review_pair")
    (root / "library").mkdir(parents=True, exist_ok=True)
    image(root / "library" / "crawled.png")
    review_set(root / "search", count=6, prefix="search")
    video(root / "search" / "clip.mp4", seconds=2.0)


if __name__ == "__main__":
    main()
