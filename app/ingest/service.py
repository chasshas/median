"""ingest(): put an incoming file into staging and queue its processing."""

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.ingest.origin import IngestOrigin
from app.jobs import defer_process_ingest
from app.storage import DataPaths, atomic_copy, atomic_move, get_paths
from app.storage.paths import crawl_batch_id, new_upload_batch_id, safe_filename


@dataclass(frozen=True)
class IngestTicket:
    batch_id: str
    staging_path: str  # relative to DATA_DIR
    job_id: int


def batch_id_for(origin: IngestOrigin) -> str:
    """run-<job_run_id> for crawls, a fresh UUID otherwise."""
    if origin.job_run_id is not None:
        return crawl_batch_id(origin.job_run_id)
    return new_upload_batch_id()


def staging_destination(paths: DataPaths, batch_id: str, filename: str) -> Path:
    directory = paths.staging_batch(batch_id)
    name = safe_filename(filename)
    dest = directory / name
    if dest.exists():
        stem, dot, ext = name.rpartition(".")
        suffix = uuid.uuid4().hex[:8]
        dest = directory / (f"{stem}-{suffix}.{ext}" if dot and stem else f"{name}-{suffix}")
    return dest


async def ingest(
    path: Path,
    origin: IngestOrigin,
    *,
    batch_id: str | None = None,
    original_filename: str | None = None,
    copy: bool = False,
    paths: DataPaths | None = None,
) -> IngestTicket:
    """Move (or copy) path into staging/<batch_id>/ and defer process_ingest for it."""
    paths = paths or get_paths()
    batch_id = batch_id or batch_id_for(origin)
    original_filename = original_filename or path.name
    dest = staging_destination(paths, batch_id, original_filename)
    await asyncio.to_thread(atomic_copy if copy else atomic_move, path, dest)
    staging_path = paths.relative(dest)
    job_id = await defer_process_ingest(
        staging_path=staging_path,
        origin=origin.model_dump(mode="json"),
        original_filename=original_filename,
        batch_id=batch_id,
    )
    return IngestTicket(batch_id=batch_id, staging_path=staging_path, job_id=job_id)
