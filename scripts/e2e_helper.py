"""App-side helper for scripts/e2e.py (phase 9). Runs where the app runs: the web container, or
the local venv with E2E_LOCAL=1. Binary input comes on stdin and binary output goes to stdout,
so the host side needs no copies into the container.

python scripts/e2e_helper.py resize <percent>          PNG/JPEG on stdin -> resized PNG on stdout
python scripts/e2e_helper.py generate <dir> <count>    distinct structured PNG images in <dir>
python scripts/e2e_helper.py crawler-ingest <source_key> <remote_id> <tag>[,<tag>...]
    ingest the file on stdin as a crawler add-on would (tags with origin crawler); prints the
    batch id
python scripts/e2e_helper.py write <path>              write stdin to <path> under DATA_DIR
"""

import asyncio
import io
import random
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from app.config import get_settings
from app.ingest.origin import IngestOrigin
from app.ingest.service import ingest
from app.jobs import open_jobs
from app.storage.fs import atomic_write_bytes


def resize(percent: int) -> None:
    img = Image.open(io.BytesIO(sys.stdin.buffer.read()))
    img.load()
    w, h = img.size
    out = img.convert("RGB").resize((max(w * percent // 100, 8), max(h * percent // 100, 8)))
    buf = io.BytesIO()
    out.save(buf, "PNG")
    sys.stdout.buffer.write(buf.getvalue())


def draw(n: int) -> Image.Image:
    """Image n: random coloured blocks, distinct enough for pHash to tell apart."""
    rng = random.Random(f"e2e-{n}")
    img = Image.new("RGB", (96, 96))
    d = ImageDraw.Draw(img)
    for _ in range(24):
        x, y = rng.randrange(96), rng.randrange(96)
        size = rng.randrange(8, 40)
        color = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        d.rectangle([x, y, x + size, y + size], fill=color)
    return img


def generate(directory: Path, count: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for n in range(1, count + 1):
        draw(n).save(directory / f"e2e-{n:05d}.png", "PNG")
    print(f"generated {count} images in {directory}")


async def crawler_ingest(source_key: str, remote_id: str, tags: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{remote_id}.png"
        path.write_bytes(sys.stdin.buffer.read())
        origin = IngestOrigin(source_key=source_key, remote_id=remote_id, tags=tags)
        async with open_jobs(get_settings().database_url):
            ticket = await ingest(path, origin, copy=True)
    print(ticket.batch_id)


def write(path: Path) -> None:
    data_dir = get_settings().data_dir.resolve()
    target = path.resolve()
    if not target.is_relative_to(data_dir):
        sys.exit(f"refusing to write outside DATA_DIR: {target}")
    atomic_write_bytes(target, sys.stdin.buffer.read())


def main(argv: list[str]) -> None:
    command, *args = argv
    if command == "resize":
        resize(int(args[0]))
    elif command == "generate":
        generate(Path(args[0]), int(args[1]))
    elif command == "crawler-ingest":
        asyncio.run(crawler_ingest(args[0], args[1], [t for t in args[2].split(",") if t]))
    elif command == "write":
        write(Path(args[0]))
    else:
        sys.exit(f"unknown command {command}")


if __name__ == "__main__":
    main(sys.argv[1:])
