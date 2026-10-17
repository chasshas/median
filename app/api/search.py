"""Search (JSON): results, parse/format for the query box and filter panel, autocomplete, and
saved searches (ARCHITECTURE.md section 12)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.search import saved
from app.search.filters import CursorError, search
from app.search.params import SearchParams, panel_fields, resolve, search_params
from app.search.query import SearchSyntaxError, format_query
from app.search.suggest import suggest
from app.search.views import result_items

router = APIRouter(tags=["search"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ParamsDep = Annotated[SearchParams, Depends(search_params)]


@router.get("/search")
async def search_files(session: SessionDep, params: ParamsDep) -> dict[str, object]:
    """Files matching q (the query syntax) or the filter fields, one keyset page at a time."""
    parsed = await resolve(session, params)
    try:
        page = await search(session, parsed.filter, params.cursor, params.limit)
    except CursorError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"message": str(exc)}) from exc
    return {
        "items": [item.as_json() for item in await result_items(session, page)],
        "next_cursor": page.next_cursor,
        "total": page.total,
        "warnings": [w.as_json() for w in parsed.warnings],
        "query": format_query(page.filter),
        "sort": page.filter.sort,
        "seed": page.filter.seed,
    }


@router.get("/search/parse")
async def parse_query(session: SessionDep, params: ParamsDep) -> dict[str, object]:
    """The filter panel's values for q (or the fields) and the canonical query string."""
    parsed = await resolve(session, params)
    return {
        "query": format_query(parsed.filter),
        "fields": panel_fields(parsed.filter),
        "warnings": [w.as_json() for w in parsed.warnings],
    }


@router.get("/search/suggest")
async def suggestions(
    session: SessionDep,
    token: str = "",
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict[str, object]:
    return {"suggestions": [s.as_json() for s in await suggest(session, token, limit)]}


# --- saved searches --------------------------------------------------------------------------


class SavedBody(BaseModel):
    name: Annotated[str, Field(max_length=200)]
    query: Annotated[str, Field(max_length=10_000)]


class SavedPatch(BaseModel):
    name: Annotated[str | None, Field(max_length=200)] = None
    query: Annotated[str | None, Field(max_length=10_000)] = None


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, saved.SavedSearchNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, SearchSyntaxError):
        return HTTPException(status.HTTP_400_BAD_REQUEST, exc.as_json())
    if isinstance(exc, saved.SavedSearchExists):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


ERRORS = (saved.SavedSearchError, saved.SavedSearchNotFound, SearchSyntaxError)


@router.get("/saved-searches")
async def list_saved(session: SessionDep) -> dict[str, object]:
    return {"saved_searches": [saved.as_json(s) for s in await saved.list_saved(session)]}


@router.post("/saved-searches", status_code=status.HTTP_201_CREATED)
async def create_saved(body: SavedBody, session: SessionDep) -> dict[str, object]:
    try:
        row = await saved.create_saved(session, body.name, body.query)
    except ERRORS as exc:
        raise _error(exc) from exc
    await session.commit()
    return saved.as_json(row)


@router.get("/saved-searches/{saved_id}")
async def get_saved(saved_id: int, session: SessionDep) -> dict[str, object]:
    try:
        return saved.as_json(await saved.get_saved(session, saved_id))
    except ERRORS as exc:
        raise _error(exc) from exc


@router.patch("/saved-searches/{saved_id}")
async def update_saved(saved_id: int, body: SavedPatch, session: SessionDep) -> dict[str, object]:
    try:
        row = await saved.update_saved(session, saved_id, body.name, body.query)
    except ERRORS as exc:
        raise _error(exc) from exc
    await session.commit()
    return saved.as_json(row)


@router.delete("/saved-searches/{saved_id}")
async def delete_saved(saved_id: int, session: SessionDep) -> dict[str, object]:
    try:
        await saved.delete_saved(session, saved_id)
    except ERRORS as exc:
        raise _error(exc) from exc
    await session.commit()
    return {"ok": True}
