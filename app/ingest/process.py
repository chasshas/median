"""process_ingest: ARCHITECTURE.md section 7, steps 1-5."""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import PurePosixPath

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models_files import File, FileSource, FileStatus, IngestEvent, IngestOutcome, MediaKind
from app.ingest.hashing import sha256_file
from app.ingest.origin import IngestOrigin
from app.ingest.perceptual import compute_perceptual
from app.ingest.probe import probe
from app.ingest.thumbs import make_thumbnail
from app.library.tags import add_user_tags, merge_crawler_tags
from app.storage import DataPaths, run_after_commit
from app.storage.fs import unlink_quietly

logger = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 2000


@dataclass(frozen=True)
class EventContext:
    """What every ingest_events row of one ingest carries."""

    source_key: str
    remote_id: str | None = None
    url: str | None = None
    job_run_id: int | None = None
    batch_id: str | None = None

    @classmethod
    def from_origin(cls, origin: IngestOrigin, batch_id: str | None) -> "EventContext":
        return cls(origin.source_key, origin.remote_id, origin.url, origin.job_run_id, batch_id)


def record_event(
    session: AsyncSession,
    outcome: IngestOutcome,
    ctx: EventContext,
    file_id: int | None = None,
    detail: str | None = None,
) -> None:
    session.add(
        IngestEvent(
            outcome=outcome,
            file_id=file_id,
            source_key=ctx.source_key,
            remote_id=ctx.remote_id,
            url=ctx.url,
            job_run_id=ctx.job_run_id,
            batch_id=ctx.batch_id,
            detail=detail,
        )
    )


async def is_known_remote(session: AsyncSession, source_key: str, remote_id: str) -> bool:
    found = await session.scalar(
        select(FileSource.id)
        .where(FileSource.source_key == source_key, FileSource.remote_id == remote_id)
        .limit(1)
    )
    return found is not None


async def attach_source(session: AsyncSession, file_id: int, origin: IngestOrigin) -> None:
    """Add a file_sources row unless this source is already recorded."""
    values = {
        "file_id": file_id,
        "source_key": origin.source_key,
        "remote_id": origin.remote_id,
        "url": origin.url,
        "source_id": origin.source_id,
        "job_run_id": origin.job_run_id,
        "raw_metadata": origin.metadata,
    }
    if origin.remote_id is not None:
        await session.execute(
            insert(FileSource)
            .values(values)
            .on_conflict_do_nothing(
                index_elements=[FileSource.source_key, FileSource.remote_id],
                index_where=FileSource.remote_id.is_not(None),
            )
        )
        return
    existing = await session.scalar(
        select(FileSource.id)
        .where(
            FileSource.file_id == file_id,
            FileSource.source_key == origin.source_key,
            FileSource.url.is_not_distinct_from(origin.url),
        )
        .limit(1)
    )
    if existing is None:
        await session.execute(insert(FileSource).values(values))


async def attach_tags(session: AsyncSession, file_id: int, origin: IngestOrigin) -> None:
    if origin.tag_origin == "user":
        await add_user_tags(session, file_id, origin.tags)
    else:
        await merge_crawler_tags(session, file_id, origin.tags)


async def process_ingest(
    sessionmaker: async_sessionmaker[AsyncSession],
    paths: DataPaths,
    staging_path: str,
    origin: IngestOrigin,
    original_filename: str | None = None,
    batch_id: str | None = None,
) -> IngestOutcome:
    """Process one staged incoming file. Returns the outcome recorded in ingest_events."""
    ctx = EventContext.from_origin(origin, batch_id)
    incoming = paths.resolve(staging_path)
    try:
        # Step 1: known remote id, checked before hashing.
        if origin.remote_id is not None and not origin.force:
            async with sessionmaker() as session:
                if await is_known_remote(session, origin.source_key, origin.remote_id):
                    record_event(session, IngestOutcome.skipped_known_remote, ctx)
                    run_after_commit(session, lambda: unlink_quietly(incoming))
                    await session.commit()
                    return IngestOutcome.skipped_known_remote

        # Step 2: SHA-256, streamed.
        sha = await asyncio.to_thread(sha256_file, incoming)
        size = (await asyncio.to_thread(incoming.stat)).st_size

        # Steps 3 and 4 (claim): the unique sha256 constraint decides new versus duplicate,
        # also when two copies of the same content are processed at once.
        async with sessionmaker() as session:
            file_id = await session.scalar(
                insert(File)
                .values(
                    sha256=sha,
                    size_bytes=size,
                    media_kind=MediaKind.other,
                    status=FileStatus.processing,
                    staging_path=staging_path,
                    original_filename=original_filename,
                    title=origin.title,
                    description=origin.description,
                    metadata_=_initial_metadata(origin),
                )
                .on_conflict_do_nothing(index_elements=[File.sha256])
                .returning(File.id)
            )
            if file_id is None:
                outcome = await _handle_duplicate(session, paths, sha, staging_path, origin, ctx)
                await session.commit()
                return outcome
            await attach_source(session, file_id, origin)
            await attach_tags(session, file_id, origin)
            await session.commit()
    except Exception as exc:
        logger.exception("ingest of %s failed before a files row existed", staging_path)
        async with sessionmaker() as session:
            record_event(session, IngestOutcome.error, ctx, detail=_message(exc))
            await session.commit()
        return IngestOutcome.error

    return await process_file(sessionmaker, paths, file_id, ctx)


def _initial_metadata(origin: IngestOrigin) -> dict[str, object]:
    if origin.default_folder_id is None:
        return {}
    return {"default_folder_id": origin.default_folder_id}


async def _handle_duplicate(
    session: AsyncSession,
    paths: DataPaths,
    sha: bytes,
    staging_path: str,
    origin: IngestOrigin,
    ctx: EventContext,
) -> IngestOutcome:
    existing = await session.scalar(select(File).where(File.sha256 == sha).with_for_update())
    assert existing is not None  # the conflicting row is committed by now
    await attach_source(session, existing.id, origin)
    await attach_tags(session, existing.id, origin)
    if origin.force and existing.status is FileStatus.discarded:
        old_trash = paths.trash_path(existing.sha_hex, existing.ext)
        existing.status = FileStatus.pending_review
        existing.staging_path = staging_path
        existing.discarded_at = None
        existing.reviewed_at = None
        record_event(
            session,
            IngestOutcome.forced_reingest,
            ctx,
            existing.id,
            detail=f"restored discarded file {existing.id}",
        )
        run_after_commit(session, lambda: unlink_quietly(old_trash))
        return IngestOutcome.forced_reingest
    record_event(
        session,
        IngestOutcome.duplicate_exact,
        ctx,
        existing.id,
        detail=f"same content as file {existing.id} ({existing.status.value})",
    )
    incoming = paths.resolve(staging_path)
    run_after_commit(session, lambda: unlink_quietly(incoming))
    return IngestOutcome.duplicate_exact


async def process_file(
    sessionmaker: async_sessionmaker[AsyncSession],
    paths: DataPaths,
    file_id: int,
    ctx: EventContext,
) -> IngestOutcome:
    """Step 4 (media work) for a claimed files row; step 5 on any exception."""
    try:
        async with sessionmaker() as session:
            file = await session.get(File, file_id)
            if file is None or file.staging_path is None:
                raise RuntimeError(f"file {file_id} has no staging file")
            path = paths.resolve(file.staging_path)
            result = await probe(path, file.original_filename)
            file.media_kind = result.media_kind
            file.mime = result.mime
            file.ext = result.ext
            file.width = result.width
            file.height = result.height
            file.duration_ms = result.duration_ms
            file.metadata_ = {**(file.metadata_ or {}), **result.metadata}
            file.has_thumb = await make_thumbnail(
                path,
                result.media_kind,
                result.duration_ms,
                paths.thumb_path(file.sha_hex),
                has_cover=bool(result.metadata.get("cover_art")),
            )
            await compute_perceptual(session, paths, file)
            file.status = FileStatus.pending_review
            file.error_message = None
            record_event(session, IngestOutcome.new, ctx, file.id)
            await session.commit()
            return IngestOutcome.new
    except Exception as exc:
        logger.exception("processing file %s failed", file_id)
        async with sessionmaker() as session:
            await session.execute(
                update(File)
                .where(File.id == file_id)
                .values(status=FileStatus.error, error_message=_message(exc))
            )
            record_event(session, IngestOutcome.error, ctx, file_id, detail=_message(exc))
            await session.commit()
        return IngestOutcome.error


class RetryError(Exception):
    pass


async def prepare_retry(session: AsyncSession, file_id: int) -> None:
    """Mark a file in status error as processing again (the caller defers retry_file)."""
    file = await session.get(File, file_id, with_for_update=True)
    if file is None:
        raise LookupError(file_id)
    if file.status is not FileStatus.error:
        raise RetryError(f"file {file_id} is {file.status.value}, not error")
    file.status = FileStatus.processing
    file.error_message = None


async def retry_context(session: AsyncSession, file_id: int) -> EventContext:
    """Event context for a retry: the file's first source and its staging batch."""
    file = await session.get(File, file_id)
    source = await session.scalar(
        select(FileSource).where(FileSource.file_id == file_id).order_by(FileSource.id).limit(1)
    )
    batch_id = None
    if file is not None and file.staging_path:
        parts = PurePosixPath(file.staging_path).parts
        if len(parts) >= 3 and parts[0] == "staging":
            batch_id = parts[1]
    if source is None:
        return EventContext("upload", batch_id=batch_id)
    return EventContext(
        source.source_key, source.remote_id, source.url, source.job_run_id, batch_id
    )


async def retry_file(
    sessionmaker: async_sessionmaker[AsyncSession], paths: DataPaths, file_id: int
) -> IngestOutcome:
    async with sessionmaker() as session:
        ctx = await retry_context(session, file_id)
    return await process_file(sessionmaker, paths, file_id, ctx)


def _message(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:MAX_ERROR_LENGTH]
