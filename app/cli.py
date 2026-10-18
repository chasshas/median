"""Command line tools.

python -m app.cli import <directory> [--tags a,b] [--force] [--recursive]
python -m app.cli rehash [--missing-only]
python -m app.cli recompute-similar
python -m app.cli purge-trash
python -m app.cli tools [--no-upgrade]
python -m app.cli backup | integrity [--verify-hashes] | housekeeping | update-tools
python -m app.cli restore [--yes] <dump file, or - for stdin>
"""

import argparse
import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path

from app.config import get_settings
from app.crawl.tools import ensure_tools
from app.db.engine import job_sessionmaker
from app.dedup import service as dedup
from app.ingest.origin import IngestOrigin
from app.ingest.service import ingest
from app.jobs import open_jobs
from app.library.tags import split_tag_input
from app.review.trash import run_purge_trash
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


async def retention_days(database_url: str) -> int:
    from app.ops.settings import load_settings

    async with job_sessionmaker(database_url) as sessionmaker, sessionmaker() as session:
        return (await load_settings(session)).trash_retention_days


async def run_job_now(name: str, **params: object) -> str:
    """Run a maintenance task in this process; it records a job_runs row like the worker."""
    from app.jobs import tasks

    async with open_jobs(get_settings().database_url):
        return await getattr(tasks, name)(job_run_id=None, **params)


def restore(target: str, yes: bool, database_url: str) -> int:
    from app.ops.backup import BackupError, restore_database, spool_stdin

    paths = get_paths()
    if target == "-":
        dump = spool_stdin(sys.stdin.buffer, paths.backups)
        cleanup = True
    else:
        dump = Path(target)
        if not dump.is_absolute() and not dump.exists():
            dump = paths.backups / target
        cleanup = False
    try:
        if not dump.is_file():
            print(f"no such dump: {target}", file=sys.stderr)
            return 2
        if not yes:
            answer = input(f"Replace the database with {dump}? Type yes to continue: ")
            if answer.strip() != "yes":
                print("cancelled")
                return 1
        try:
            entries = restore_database(database_url, dump)
        except BackupError as exc:
            print(f"restore failed: {exc}", file=sys.stderr)
            return 1
        print(f"restored {entries} entries from {target}")
        return 0
    finally:
        if cleanup:
            dump.unlink(missing_ok=True)


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
    sub.add_parser(
        "purge-trash", help="delete trash originals discarded more than TRASH_RETENTION_DAYS ago"
    )
    tools = sub.add_parser(
        "tools", help="install or upgrade yt-dlp and gallery-dl in DATA_DIR/tools/venv"
    )
    tools.add_argument(
        "--no-upgrade", action="store_true", help="only install when the venv is missing"
    )
    sub.add_parser("backup", help="dump the database to DATA_DIR/backups (recorded as a job run)")
    integrity = sub.add_parser("integrity", help="compare the database with DATA_DIR")
    integrity.add_argument(
        "--verify-hashes", action="store_true", help="also re-hash every archive file"
    )
    sub.add_parser("housekeeping", help="delete old job runs and logs, measure disk usage")
    sub.add_parser("update-tools", help="upgrade the crawler tools (recorded as a job run)")
    rest = sub.add_parser("restore", help="replace the app database with a backup dump")
    rest.add_argument("file", help="dump file (absolute, or a name in DATA_DIR/backups), or -")
    rest.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)
    if args.command == "tools":
        versions = ensure_tools(get_paths(), upgrade=not args.no_upgrade)
        print_stats("tools", versions)
        return 1 if versions.get("error") else 0
    database_url = get_settings().database_url

    if args.command == "restore":
        return restore(args.file, args.yes, database_url)
    jobs = {"backup": "backup", "integrity": "integrity", "housekeeping": "housekeeping"}
    jobs["update-tools"] = "update_tools"
    if args.command in jobs:
        params = {"verify_hashes": True} if getattr(args, "verify_hashes", False) else {}
        result = asyncio.run(run_job_now(jobs[args.command], **params))
        print(f"{args.command}: {result} (details on /jobs)")
        return 0 if result == "succeeded" else 1

    if args.command == "rehash":
        stats = asyncio.run(run_rehash(args.missing_only, database_url))
        print_stats("rehash", stats.as_dict())
        return 1 if stats.errors else 0
    if args.command == "recompute-similar":
        print_stats("recompute-similar", asyncio.run(run_recompute_similar(database_url)).as_dict())
        return 0

    if args.command == "purge-trash":
        days = asyncio.run(retention_days(database_url))
        stats = asyncio.run(run_purge_trash(database_url, get_paths(), days))
        print_stats("purge-trash", stats.as_dict())
        return 1 if stats.errors else 0

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
