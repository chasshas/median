"""Folders (JSON): tree with counts, recent folders, path lookup, create, rename, move, delete."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.models_library import Folder
from app.library import folders

router = APIRouter(prefix="/folders", tags=["folders"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class CreateBody(BaseModel):
    name: str
    parent_id: int | None = None  # None = top level


class UpdateBody(BaseModel):
    name: str | None = None  # rename
    parent_id: int | None = None  # move (only when sent; null = top level)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, folders.FolderNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


async def folder_json(session: AsyncSession, folder: Folder) -> dict[str, object]:
    return {
        "id": folder.id,
        "parent_id": folder.parent_id,
        "name": folder.name,
        "path": await folders.path_of(session, folder.id),
        "is_inbox": folders.is_inbox(folder),
    }


@router.get("")
async def tree(session: SessionDep) -> dict[str, object]:
    """Every folder, nested, with direct and subtree counts of archived files."""
    return {"folders": [node.as_json() for node in await folders.get_tree(session)]}


@router.get("/recent")
async def recent(
    session: SessionDep, limit: Annotated[int, Query(ge=1, le=50)] = 5
) -> dict[str, object]:
    """Folders that most recently received archived files, newest first."""
    found = await folders.recent_folders(session, limit)
    return {"folders": [{"id": f.id, "path": f.path} for f in found]}


@router.get("/resolve")
async def resolve(session: SessionDep, path: str) -> dict[str, object]:
    """The folder for a path like "A/B" (case-insensitive)."""
    folder_id = await folders.resolve_path(session, path)
    if folder_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no folder {path!r}")
    return await folder_json(session, await folders.get_folder(session, folder_id))


@router.get("/{folder_id}")
async def get(folder_id: int, session: SessionDep) -> dict[str, object]:
    try:
        folder = await folders.get_folder(session, folder_id)
    except folders.FolderNotFound as exc:
        raise _error(exc) from exc
    body = await folder_json(session, folder)
    body["subtree_ids"] = await folders.subtree_ids(session, folder_id)
    return body


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(body: CreateBody, session: SessionDep) -> dict[str, object]:
    try:
        folder = await folders.create(session, body.name, body.parent_id)
        result = await folder_json(session, folder)
    except (folders.FolderError, folders.FolderNotFound) as exc:
        raise _error(exc) from exc
    await session.commit()
    return result


@router.patch("/{folder_id}")
async def change(folder_id: int, body: UpdateBody, session: SessionDep) -> dict[str, object]:
    """Rename (name) and/or move (parent_id; null moves to the top level)."""
    try:
        folder = await folders.get_folder(session, folder_id)
        if "parent_id" in body.model_fields_set:
            folder = await folders.move(session, folder_id, body.parent_id)
        if body.name is not None:
            folder = await folders.rename(session, folder_id, body.name)
        result = await folder_json(session, folder)
    except (folders.FolderError, folders.FolderNotFound) as exc:
        raise _error(exc) from exc
    await session.commit()
    return result


@router.delete("/{folder_id}")
async def delete(
    folder_id: int,
    session: SessionDep,
    move_contents_to_parent: Annotated[bool, Query()] = False,
) -> dict[str, object]:
    """Delete a folder; a non-empty one (409 otherwise) needs move_contents_to_parent=true."""
    try:
        result = await folders.delete(session, folder_id, move_contents_to_parent)
    except (folders.FolderError, folders.FolderNotFound) as exc:
        raise _error(exc) from exc
    await session.commit()
    return {"deleted": folder_id, **result.as_json()}
