"""Generate unique test media for scripts/smoke.sh: python scripts/smoke_media.py <dir>.

<dir>/upload: 3 PNG images and 1 MP4 video.
<dir>/import: 2 images, a video, an MP3, a text file and a corrupt JPEG (one in a subdir).
Every run produces new content (random pixels and metadata), so repeated runs stay "new".
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

from PIL import Image


def image(path: Path) -> None:
    Image.frombytes("RGB", (96, 64), os.urandom(96 * 64 * 3)).save(path)


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


if __name__ == "__main__":
    main()
