"""Per-file tags and details (JSON): /api/files/<id>/tags and PATCH /api/files/<id>."""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.models_files import File
from app.library import classify, tags
from app.library.folders import FolderNotFound

router = APIRouter(prefix="/files", tags=["files"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SetFn = Callable[[AsyncSession, int, int], Awaitable[bool]]


class AddTagBody(BaseModel):
    name: str


class DetailsBody(BaseModel):
    title: str | None = None
    description: str | None = None
    folder_id: int | None = None


async def _file(session: AsyncSession, file_id: int) -> File:
    file = await session.get(File, file_id)
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    return file


async def tags_json(session: AsyncSession, file_id: int) -> dict[str, object]:
    rows = await tags.file_tags(session, file_id)
    return {
        "file_id": file_id,
        "active": [r.as_json() for r in rows if not r.removed],
        "removed": [r.as_json() for r in rows if r.removed],
    }


@router.get("/{file_id}/tags")
async def get_tags(file_id: int, session: SessionDep) -> dict[str, object]:
    """Active tags and removed ones (kept so recrawls never re-add them), by name."""
    await _file(session, file_id)
    return await tags_json(session, file_id)


@router.post("/{file_id}/tags")
async def add_tag(file_id: int, body: AddTagBody, session: SessionDep) -> dict[str, object]:
    """Add a tag as the user (a removed row becomes active with origin user)."""
    await _file(session, file_id)
    try:
        await tags.add_user_tag(session, file_id, body.name)
    except tags.TagError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await session.commit()
    return await tags_json(session, file_id)


async def _set(session: AsyncSession, file_id: int, tag_id: int, fn: SetFn) -> dict[str, object]:
    await _file(session, file_id)
    if not await fn(session, file_id, tag_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"file {file_id} has no tag {tag_id}")
    await session.commit()
    return await tags_json(session, file_id)


@router.delete("/{file_id}/tags/{tag_id}")
async def remove_tag(file_id: int, tag_id: int, session: SessionDep) -> dict[str, object]:
    """Mark the tag removed on this file (any origin)."""
    return await _set(session, file_id, tag_id, tags.remove_tag)


@router.post("/{file_id}/tags/{tag_id}/restore")
async def restore_tag(file_id: int, tag_id: int, session: SessionDep) -> dict[str, object]:
    """Make a removed tag active again (its origin stays)."""
    return await _set(session, file_id, tag_id, tags.restore_tag)


@router.patch("/{file_id}")
async def update_details(file_id: int, body: DetailsBody, session: SessionDep) -> dict[str, object]:
    """Change the title, the description (empty = none) or the folder of an archived file."""
    changes = {k: getattr(body, k) for k in body.model_fields_set}
    try:
        file = await classify.update_details(session, file_id, changes)
    except LookupError as exc:
        code = 400 if isinstance(exc, FolderNotFound) else status.HTTP_404_NOT_FOUND
        raise HTTPException(code, str(exc)) from exc
    except classify.ClassifyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    result = {
        "id": file.id,
        "title": file.title,
        "description": file.description,
        "folder_id": file.folder_id,
    }
    await session.commit()
    return result
