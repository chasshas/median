"""Command line tools.

python -m app.cli import <directory> [--tags a,b] [--force] [--recursive]
python -m app.cli rehash [--missing-only]
python -m app.cli recompute-similar
"""

import argparse
import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path

from app.config import get_settings
from app.db.engine import job_sessionmaker
from app.dedup import service as dedup
from app.ingest.origin import IngestOrigin
from app.ingest.service import ingest
from app.jobs import open_jobs
from app.library.tags import split_tag_input
from app.storage import get_paths
from app.storage.paths import new_upload_batch_id


def iter_files(directory: Path, recursive: bool) -> Iterator[Path]:
    """Regular files in directory (sorted), skipping hidden files and directories."""
    candidates = directory.rglob("*") if recursive else directory.iterdir()
    for path in sorted(candidates):
        rel = path.relative_to(directory)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if path.is_file() and not path.is_symlink():
            yield path


async def import_directory(
    directory: Path, *, tags: list[str], force: bool, recursive: bool, database_url: str
) -> tuple[str, int]:
    """Copy every file of directory into staging and queue it. The source files are kept."""
    paths = get_paths()
    origin = IngestOrigin(tags=tags, tag_origin="user", force=force)
    batch_id = new_upload_batch_id()
    count = 0
    async with open_jobs(database_url):
        for path in iter_files(directory, recursive):
            await ingest(path, origin, batch_id=batch_id, copy=True, paths=paths)
            count += 1
            print(f"queued {path}", flush=True)
    return batch_id, count


async def run_rehash(missing_only: bool, database_url: str) -> dedup.RehashStats:
    async with job_sessionmaker(database_url) as sessionmaker:
        return await dedup.rehash(sessionmaker, get_paths(), missing_only=missing_only)


async def run_recompute_similar(database_url: str) -> dedup.RecomputeStats:
    async with job_sessionmaker(database_url) as sessionmaker:
        return await dedup.recompute_similar(sessionmaker)


def print_stats(title: str, stats: dict[str, object]) -> None:
    print(f"{title}: " + ", ".join(f"{key} {value}" for key, value in stats.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Median tools")
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="ingest every file of a directory (copies, keeps them)")
    imp.add_argument("directory", type=Path)
    imp.add_argument("--tags", default="", help="comma separated tags, added as user tags")
    imp.add_argument("--force", action="store_true", help="re-ingest previously discarded files")
    imp.add_argument("--recursive", action="store_true", help="include subdirectories")
    rehash = sub.add_parser(
        "rehash", help="recompute perceptual hashes from the originals and rebuild their pairs"
    )
    rehash.add_argument(
        "--missing-only", action="store_true", help="only images and videos without hashes"
    )
    sub.add_parser(
        "recompute-similar", help="rebuild similar pairs from stored hashes (current thresholds)"
    )
    args = parser.parse_args(argv)
    database_url = get_settings().database_url

    if args.command == "rehash":
        stats = asyncio.run(run_rehash(args.missing_only, database_url))
        print_stats("rehash", stats.as_dict())
        return 1 if stats.errors else 0
    if args.command == "recompute-similar":
        print_stats("recompute-similar", asyncio.run(run_recompute_similar(database_url)).as_dict())
        return 0

    if args.command == "import":
        if not args.directory.is_dir():
            parser.error(f"not a directory: {args.directory}")
        if args.directory.resolve().is_relative_to(get_paths().root.resolve()):
            parser.error("refusing to import from inside DATA_DIR")
        try:
            batch_id, count = asyncio.run(
                import_directory(
                    args.directory,
                    tags=split_tag_input(args.tags),
                    force=args.force,
                    recursive=args.recursive,
                    database_url=database_url,
                )
            )
        except KeyboardInterrupt:
            return 130
        print(f"batch {batch_id}: queued {count} file(s); the worker processes them.")
        print(f"progress: GET /api/uploads/{batch_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
