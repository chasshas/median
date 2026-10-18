"""Integrity check: compare the database with DATA_DIR and report what does not match.

The report goes to job_runs.stats and the run's page. Only three fixes exist, all safe:
regenerate missing thumbnails, ingest orphan staging files, delete leftover work directories.
Orphan archive files and hash mismatches are reported and never touched.
"""

import asyncio
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models_crawl import JobRun, JobStatus
from app.db.models_files import File, FileStatus
from app.ingest.hashing import sha256_file
from app.ingest.origin import IngestOrigin
from app.ingest.service import ingest
from app.ingest.thumbs import make_thumbnail
from app.storage import DataPaths
from app.storage.fs import remove_tree
from app.storage.paths import new_upload_batch_id

logger = logging.getLogger(__name__)

# Problem kinds in report order, with the label the job page shows.
PROBLEMS: dict[str, str] = {
    "missing_archive": "Archived files whose original is missing from archive/",
    "missing_staging": "Pending or failed files whose staging file is missing",
    "missing_trash": "Discarded files whose trash copy is missing before the retention ends",
    "orphan_archive": "Files in archive/ without a matching archived row (never deleted)",
    "orphan_staging": "Files in staging/ without a row",
    "leftover_work": "Directories in work/ without a running job",
    "missing_thumb": "Thumbnails missing from thumbs/",
    "hash_mismatch": "Archive files whose SHA-256 differs from the database (never deleted)",
}
FIXES = {"thumbs": "missing_thumb", "staging": "orphan_staging", "work": "leftover_work"}
MAX_LISTED = 500  # entries kept per problem kind in the report (the count is always exact)
MIN_ORPHAN_AGE_SECONDS = 3600  # younger staging files may still be on their way in


@dataclass
class Report:
    verify_hashes: bool = False
    checked: dict[str, int] = field(default_factory=dict)
    problems: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {kind: [] for kind in PROBLEMS}
    )
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(PROBLEMS, 0))

    def add(self, kind: str, entry: dict[str, Any]) -> None:
        self.counts[kind] += 1
        if len(self.problems[kind]) < MAX_LISTED:
            self.problems[kind].append(entry)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "verify_hashes": self.verify_hashes,
            "checked": self.checked,
            "counts": self.counts,
            "total_problems": self.total,
            "problems": self.problems,
        }


def _walk_files(directory: Path) -> Iterator[Path]:
    if not directory.is_dir():
        return
    for path in sorted(directory.rglob("*")):
        if path.is_file() or path.is_symlink():
            yield path


def _rel(paths: DataPaths, path: Path) -> str:
    return path.relative_to(paths.root).as_posix()


async def queued_staging_paths(session: AsyncSession) -> set[str]:
    """Staging files that a waiting or running process_ingest job will pick up."""
    exists = await session.scalar(text("SELECT to_regclass('public.procrastinate_jobs')"))
    if exists is None:
        return set()
    rows = await session.scalars(
        text(
            "SELECT args->>'staging_path' FROM procrastinate_jobs"
            " WHERE task_name = 'process_ingest' AND status IN ('todo', 'doing')"
        )
    )
    return {r for r in rows.all() if r}


async def running_run_ids(session: AsyncSession) -> set[int]:
    rows = await session.scalars(select(JobRun.id).where(JobRun.status == JobStatus.running))
    return set(rows.all())


def _is_old(path: Path, now: float) -> bool:
    try:
        return now - path.lstat().st_mtime >= MIN_ORPHAN_AGE_SECONDS
    except OSError:
        return False


async def check(
    sessionmaker: async_sessionmaker[AsyncSession],
    paths: DataPaths,
    *,
    retention_days: int,
    verify_hashes: bool = False,
    log: logging.Logger = logger,
    now: datetime | None = None,
) -> Report:
    now = now or datetime.now(UTC)
    report = Report(verify_hashes=verify_hashes)
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(
                    File.id,
                    File.sha256,
                    File.ext,
                    File.status,
                    File.staging_path,
                    File.has_thumb,
                    File.discarded_at,
                )
            )
        ).all()
        queued = await queued_staging_paths(session)
        running = await running_run_ids(session)
    report.checked["files"] = len(rows)
    retention_start = now - timedelta(days=retention_days)
    archived: dict[str, tuple[int, bytes]] = {}  # relative archive path -> (id, sha)
    staging_rows: set[str] = set()

    for file_id, sha, ext, status, staging_path, has_thumb, discarded_at in rows:
        sha_hex = sha.hex()
        if staging_path:
            staging_rows.add(staging_path)
        if status is FileStatus.archived:
            path = paths.archive_path(sha_hex, ext)
            archived[_rel(paths, path)] = (file_id, sha)
            if not path.is_file():
                report.add("missing_archive", {"file_id": file_id, "path": _rel(paths, path)})
        elif status in (FileStatus.pending_review, FileStatus.error):
            if not staging_path or not _staging_exists(paths, staging_path):
                report.add(
                    "missing_staging",
                    {"file_id": file_id, "path": staging_path, "status": status.value},
                )
        elif status is FileStatus.discarded:
            if discarded_at is not None and discarded_at >= retention_start:
                path = paths.trash_path(sha_hex, ext)
                if not path.is_file():
                    report.add("missing_trash", {"file_id": file_id, "path": _rel(paths, path)})
        if has_thumb and not paths.thumb_path(sha_hex).is_file():
            report.add(
                "missing_thumb",
                {"file_id": file_id, "path": _rel(paths, paths.thumb_path(sha_hex))},
            )

    count = 0
    for path in _walk_files(paths.archive):
        count += 1
        rel = _rel(paths, path)
        if rel not in archived:
            report.add("orphan_archive", {"path": rel, "size_bytes": path.lstat().st_size})
    report.checked["archive_files"] = count

    count = 0
    clock = time.time()
    for path in _walk_files(paths.staging):
        count += 1
        rel = _rel(paths, path)
        if rel in staging_rows or rel in queued or not _is_old(path, clock):
            continue
        report.add(
            "orphan_staging",
            {"path": rel, "size_bytes": path.lstat().st_size, "partial": path.name.startswith(".")},
        )
    report.checked["staging_files"] = count

    count = 0
    if paths.work.is_dir():
        for entry in sorted(paths.work.iterdir()):
            count += 1
            if entry.name.isdigit() and int(entry.name) in running:
                continue
            report.add("leftover_work", {"path": _rel(paths, entry), "name": entry.name})
    report.checked["work_dirs"] = count

    if verify_hashes:
        hashed = 0
        for rel, (file_id, sha) in sorted(archived.items()):
            path = paths.root / rel
            if not path.is_file():
                continue  # already reported as missing
            actual = await asyncio.to_thread(sha256_file, path)
            hashed += 1
            if actual != sha:
                report.add(
                    "hash_mismatch",
                    {
                        "file_id": file_id,
                        "path": rel,
                        "expected": sha.hex(),
                        "actual": actual.hex(),
                    },
                )
        report.checked["hashed"] = hashed

    for kind, n in report.counts.items():
        if n:
            log.warning("%s: %d", kind, n)
    log.info("integrity check: %d problems (%s)", report.total, report.checked)
    return report


def _staging_exists(paths: DataPaths, staging_path: str) -> bool:
    try:
        return paths.resolve(staging_path).is_file()
    except ValueError:
        return False


# Fixes ---------------------------------------------------------------------------------------


async def regenerate_thumbs(
    sessionmaker: async_sessionmaker[AsyncSession], paths: DataPaths, file_ids: list[int]
) -> dict[str, Any]:
    """Recreate missing thumbnails from the originals (files whose thumbnail is still missing)."""
    done, failed, skipped = [], [], 0
    for file_id in file_ids:
        async with sessionmaker() as session:
            file = await session.get(File, file_id)
            if file is None or paths.thumb_path(file.sha_hex).is_file():
                skipped += 1
                continue
            original = paths.original_path(
                file.status.value, file.staging_path, file.sha_hex, file.ext
            )
            if original is None or not original.is_file():
                failed.append(file_id)
                continue
            ok = await make_thumbnail(
                original,
                file.media_kind,
                file.duration_ms,
                paths.thumb_path(file.sha_hex),
                has_cover=bool((file.metadata_ or {}).get("cover_art")),
            )
            file.has_thumb = ok
            await session.commit()
            (done if ok else failed).append(file_id)
    return {"regenerated": len(done), "failed": failed, "skipped": skipped}


async def ingest_orphans(
    sessionmaker: async_sessionmaker[AsyncSession], paths: DataPaths, rel_paths: list[str]
) -> dict[str, Any]:
    """Hand orphan staging files to the ingest service again (one new upload batch).

    Each file is checked again first: still there, still without a row or a queued job, not
    a partial upload (names starting with a dot)."""
    async with sessionmaker() as session:
        rows = await session.scalars(
            select(File.staging_path).where(File.staging_path.in_(rel_paths))
        )
        referenced = set(rows.all()) | await queued_staging_paths(session)
    batch_id = new_upload_batch_id()
    ingested, skipped = [], []
    for rel in rel_paths:
        try:
            path = paths.resolve(rel)
        except ValueError:
            skipped.append(rel)
            continue
        inside = path.is_relative_to(paths.staging.resolve())
        if not inside or rel in referenced or path.name.startswith(".") or not path.is_file():
            skipped.append(rel)
            continue
        await ingest(path, IngestOrigin(tag_origin="user"), batch_id=batch_id, paths=paths)
        ingested.append(rel)
    return {"ingested": len(ingested), "skipped": skipped, "batch_id": batch_id}


async def delete_work_dirs(
    sessionmaker: async_sessionmaker[AsyncSession], paths: DataPaths, names: list[str]
) -> dict[str, Any]:
    """Remove leftover work/<name> directories whose run is not running (checked again)."""
    async with sessionmaker() as session:
        running = await running_run_ids(session)
    deleted, skipped = [], []
    for name in names:
        target = paths.work / name
        valid = name and "/" not in name and name not in {".", ".."} and target.exists()
        if not valid or (name.isdigit() and int(name) in running):
            skipped.append(name)
            continue
        if target.is_dir() and not target.is_symlink():
            remove_tree(target)
        else:
            target.unlink(missing_ok=True)
        deleted.append(name)
    return {"deleted": len(deleted), "skipped": skipped}


def fix_targets(stats: dict[str, Any], action: str) -> list[Any]:
    """What a fix applies to, taken from a stored report."""
    entries = (stats.get("problems") or {}).get(FIXES[action]) or []
    if action == "thumbs":
        return [e["file_id"] for e in entries if "file_id" in e]
    if action == "staging":
        return [e["path"] for e in entries if "path" in e]
    return [e["name"] for e in entries if "name" in e]


async def apply_fix(
    sessionmaker: async_sessionmaker[AsyncSession], paths: DataPaths, run_id: int, action: str
) -> dict[str, Any]:
    """Run one safe fix for the problems in an integrity run's report; the result is stored
    under stats.fixes.<action>."""
    if action not in FIXES:
        raise ValueError(f"unknown fix {action!r}")
    async with sessionmaker() as session:
        run = await session.get(JobRun, run_id)
        if run is None or run.kind.value != "integrity" or run.status is not JobStatus.succeeded:
            raise LookupError(run_id)
        targets = fix_targets(run.stats, action)
    if action == "thumbs":
        result = await regenerate_thumbs(sessionmaker, paths, targets)
    elif action == "staging":
        result = await ingest_orphans(sessionmaker, paths, targets)
    else:
        result = await delete_work_dirs(sessionmaker, paths, targets)
    result["at"] = datetime.now(UTC).isoformat()
    async with sessionmaker() as session:
        run = await session.get(JobRun, run_id, with_for_update=True)
        if run is not None:
            fixes = dict(run.stats.get("fixes") or {})
            fixes[action] = result
            run.stats = {**run.stats, "fixes": fixes}
            await session.commit()
    return result
