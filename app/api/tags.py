"""Tags (JSON): list with counts, autocomplete, rename (merge), delete."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.library import tags

router = APIRouter(prefix="/tags", tags=["tags"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class RenameBody(BaseModel):
    name: str
    merge: bool = False  # required when a tag with the new name exists


@router.get("")
async def list_tags(session: SessionDep, q: str | None = None) -> dict[str, object]:
    """Every tag with its usage count (active rows), by name; q filters by substring."""
    return {"tags": [t.as_json() for t in await tags.list_tags(session, q)]}


@router.get("/autocomplete")
async def autocomplete(
    session: SessionDep,
    prefix: str = "",
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict[str, object]:
    """Tags starting with the normalized prefix, most used first, then by name."""
    return {"tags": [t.as_json() for t in await tags.autocomplete(session, prefix, limit)]}


@router.post("/{tag_id}/rename", response_model=None)
async def rename(
    tag_id: int, body: RenameBody, session: SessionDep
) -> dict[str, object] | JSONResponse:
    """Rename; if the new name exists, 409 with merge_into unless merge is true."""
    try:
        current = await tags.get_tag(session, tag_id)
        name = tags.valid_tag(body.name)
        target = await tags.find_tag(session, name)
        if target is not None and target.id != tag_id and not body.merge:
            return JSONResponse(
                {
                    "detail": f'"{name}" already exists ({target.count} file(s)); '
                    f'renaming merges "{current.name}" into it',
                    "merge_into": target.as_json(),
                },
                status_code=status.HTTP_409_CONFLICT,
            )
        result = await tags.rename_tag(session, tag_id, name)
    except tags.TagNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except tags.TagError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await session.commit()
    return {"tag": result.tag.as_json(), "merged_from": result.merged_from}


@router.delete("/{tag_id}")
async def delete(
    tag_id: int, session: SessionDep, force: Annotated[bool, Query()] = False
) -> dict[str, object]:
    """Delete a tag; one in use needs force=true (409 otherwise)."""
    try:
        usage = await tags.delete_tag(session, tag_id, force)
    except tags.TagNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except tags.TagInUse as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await session.commit()
    return {"deleted": usage.as_json()}
