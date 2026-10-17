"""Library (JSON): archived files of a folder, folder moves and bulk tag changes."""

from collections.abc import Awaitable, Callable, Iterable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.models_files import File
from app.library import classify
from app.library.browse import (
    MAX_PER_PAGE,
    PER_PAGE,
    Direction,
    LibraryContext,
    LibrarySort,
    library_page,
)
from app.library.folders import FolderNotFound

router = APIRouter(prefix="/library", tags=["library"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
BulkTagFn = Callable[[AsyncSession, Iterable[int], Iterable[str]], Awaitable[classify.BulkResult]]
FileIds = Annotated[list[int], Field(min_length=1, max_length=1000)]


def _flag(value: str | None) -> bool:
    return (value or "").lower() in ("1", "true", "on", "yes")


def library_context(
    folder: str | None = None,
    sub: str | None = None,
    sort: LibrarySort = "archived",
    dir: Direction | None = None,
) -> LibraryContext:
    """Context from query parameters; empty values (plain HTML forms) mean the default."""
    if folder:
        try:
            folder_id: int | None = int(folder)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "folder must be an id") from exc
    else:
        folder_id = None
    return LibraryContext(folder_id, _flag(sub), sort, dir or None)


ContextDep = Annotated[LibraryContext, Depends(library_context)]


class MoveBody(BaseModel):
    file_ids: FileIds
    folder_id: int


class TagsBody(BaseModel):
    file_ids: FileIds
    tags: Annotated[list[str], Field(min_length=1)]


def file_json(f: File) -> dict[str, object]:
    return {
        "id": f.id,
        "title": f.title,
        "original_filename": f.original_filename,
        "media_kind": f.media_kind.value,
        "size_bytes": f.size_bytes,
        "width": f.width,
        "height": f.height,
        "duration_ms": f.duration_ms,
        "folder_id": f.folder_id,
        "collected_at": f.collected_at.isoformat(),
        "archived_at": f.archived_at.isoformat() if f.archived_at else None,
        "thumb_url": f"/media/{f.id}/thumb" if f.has_thumb else None,
        "original_url": f"/media/{f.id}/original",
    }


@router.get("")
async def files(
    session: SessionDep,
    ctx: ContextDep,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=MAX_PER_PAGE)] = PER_PAGE,
) -> dict[str, object]:
    """Archived files of a folder (folder=<id>, sub=1 with subfolders; none = every folder)."""
    result = await library_page(session, ctx, page, per_page)
    return {
        "items": [file_json(f) for f in result.files],
        "total": result.total,
        "page": result.page,
        "pages": result.pages,
    }


@router.post("/move")
async def move(body: MoveBody, session: SessionDep) -> dict[str, object]:
    """Put archived files into a folder; the originals stay where they are."""
    try:
        result = await classify.move_files(session, body.file_ids, body.folder_id)
    except FolderNotFound as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await session.commit()
    return result.as_json()


async def _tags(session: AsyncSession, body: TagsBody, fn: BulkTagFn) -> dict[str, object]:
    try:
        result = await fn(session, body.file_ids, body.tags)
    except classify.ClassifyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await session.commit()
    return result.as_json()


@router.post("/tags/add")
async def add_tags(body: TagsBody, session: SessionDep) -> dict[str, object]:
    return await _tags(session, body, classify.add_tags_to_files)


@router.post("/tags/remove")
async def remove_tags(body: TagsBody, session: SessionDep) -> dict[str, object]:
    return await _tags(session, body, classify.remove_tags_from_files)
