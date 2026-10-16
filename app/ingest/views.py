"""Read-side queries for the upload, review and file pages."""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_files import File, FileSource, FileStatus
from app.db.models_library import FileTag, Tag


@dataclass
class FileTagView:
    name: str
    origin: str


@dataclass
class FileDetail:
    file: File
    sources: list[FileSource]
    tags: list[FileTagView]


async def status_counts(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(select(File.status, func.count()).group_by(File.status))
    found = {status.value: count for status, count in rows.all()}
    return {status.value: found.get(status.value, 0) for status in FileStatus}


async def files_with_status(session: AsyncSession, status: FileStatus, limit: int) -> list[File]:
    result = await session.scalars(
        select(File).where(File.status == status).order_by(File.id.desc()).limit(limit)
    )
    return list(result.all())


async def file_detail(session: AsyncSession, file_id: int) -> FileDetail | None:
    file = await session.get(File, file_id)
    if file is None:
        return None
    sources = await session.scalars(
        select(FileSource).where(FileSource.file_id == file_id).order_by(FileSource.id)
    )
    tags = await session.execute(
        select(Tag.name, FileTag.origin)
        .join(FileTag, FileTag.tag_id == Tag.id)
        .where(FileTag.file_id == file_id, FileTag.removed.is_(False))
        .order_by(Tag.name)
    )
    return FileDetail(
        file,
        list(sources.all()),
        [FileTagView(name, origin.value) for name, origin in tags.all()],
    )
