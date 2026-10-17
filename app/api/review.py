"""Review queue and decisions (JSON). Decisions return one result per file."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.models_files import FileStatus, MediaKind
from app.review import service
from app.review.queue import QueueError, QueueFilters, review_queue
from app.storage import get_paths

router = APIRouter(prefix="/review", tags=["review"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
FileIds = Annotated[list[int], Field(min_length=1, max_length=1000)]


class ArchiveBody(BaseModel):
    file_ids: FileIds
    folder_id: int | None = None  # default: Inbox
    tags: list[str] = []


class FileIdsBody(BaseModel):
    file_ids: FileIds


class ReplaceBody(BaseModel):
    new_id: int
    old_id: int


class UndoBody(BaseModel):
    action_ids: Annotated[list[int], Field(min_length=1, max_length=1000)]


class CompareBody(BaseModel):
    file_id: int  # the pending file (left)
    other_id: int  # the similar file (right)
    choice: service.CompareChoice
    folder_id: int | None = None
    tags: list[str] = []


def _tristate(name: str, value: str | None) -> bool | None:
    """Query flag from a form select: "" or missing = any, true/false otherwise."""
    if value is None or value == "":
        return None
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no"):
        return False
    raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{name} must be true or false")


def queue_filters(
    status_: Annotated[
        Literal["pending_review", "error"], Query(alias="status")
    ] = "pending_review",
    media_kind: Annotated[list[MediaKind] | None, Query()] = None,
    source_key: Annotated[list[str] | None, Query()] = None,
    batch: str | None = None,
    has_similar: str | None = None,
    similar_to_discarded: str | None = None,
    order: Literal["oldest", "newest"] = "oldest",
) -> QueueFilters:
    try:
        return QueueFilters(
            status=FileStatus(status_),
            media_kinds=tuple(media_kind or ()),
            source_keys=tuple(k for k in source_key or () if k),
            batch=batch or None,
            has_similar=_tristate("has_similar", has_similar),
            similar_to_discarded=_tristate("similar_to_discarded", similar_to_discarded),
            order=order,
        )
    except QueueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/queue")
async def queue(
    session: SessionDep,
    filters: Annotated[QueueFilters, Depends(queue_filters)],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, object]:
    """Pending files (or status=error ones), oldest collected first unless order=newest."""
    try:
        page = await review_queue(session, filters, cursor or None, limit)
    except QueueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"items": page.items, "total": page.total, "next_cursor": page.next_cursor}


async def _finish(session: AsyncSession, result: service.ReviewResult) -> dict[str, object]:
    await session.commit()
    return result.as_json()


@router.post("/archive")
async def archive(body: ArchiveBody, session: SessionDep) -> dict[str, object]:
    try:
        result = await service.archive(
            session, get_paths(), body.file_ids, body.folder_id, body.tags
        )
    except service.ReviewError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return await _finish(session, result)


@router.post("/discard")
async def discard(body: FileIdsBody, session: SessionDep) -> dict[str, object]:
    return await _finish(session, await service.discard(session, get_paths(), body.file_ids))


@router.post("/discard-archived")
async def discard_archived(body: FileIdsBody, session: SessionDep) -> dict[str, object]:
    result = await service.discard_archived(session, get_paths(), body.file_ids)
    return await _finish(session, result)


@router.post("/replace")
async def replace(body: ReplaceBody, session: SessionDep) -> dict[str, object]:
    result = await service.replace(session, get_paths(), body.new_id, body.old_id)
    return await _finish(session, result)


@router.post("/compare")
async def compare(body: CompareBody, session: SessionDep) -> dict[str, object]:
    """One decision from compare mode, all or nothing (409 with the reason otherwise)."""
    try:
        result = await service.compare_decision(
            session,
            get_paths(),
            body.file_id,
            body.other_id,
            body.choice,
            body.folder_id,
            body.tags,
        )
    except service.ReviewError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await _finish(session, result)


@router.post("/undo")
async def undo(body: UndoBody, session: SessionDep) -> dict[str, object]:
    try:
        result = await service.undo(session, get_paths(), body.action_ids)
    except service.ReviewError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await _finish(session, result)
