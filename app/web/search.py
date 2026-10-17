"""The search page (ARCHITECTURE.md sections 12 and 13).

The URL always holds the query: /search?q=...&sort=...&seed=... A submit of the filter panel
(filter fields instead of q) is redirected to the formatted query, and a random sort without a
seed is redirected to one with a seed, so every result page can be bookmarked.
"""

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.models_files import MediaKind
from app.library.folders import folder_choices
from app.search.filters import SORT_LABELS, SORTS, CursorError, SearchFilter, search
from app.search.params import SearchParams, panel_fields, resolve, search_params
from app.search.query import STATUS_WORDS, format_query
from app.search.saved import list_saved
from app.search.views import result_items
from app.web.pages import render

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ParamsDep = Annotated[SearchParams, Depends(search_params)]


def search_url(
    q: str, sort: str = "archived_desc", seed: int | None = None, **extra: object
) -> str:
    params: dict[str, object] = {"q": q}
    if sort != "archived_desc":
        params["sort"] = sort
    if sort == "random" and seed is not None:
        params["seed"] = seed
    params.update({k: v for k, v in extra.items() if v})
    return "/search?" + urlencode(params)


def _start(value: str | None) -> int:
    """How many results came before this page (only for the "showing a-b" line)."""
    try:
        return max(0, int(value or 0))
    except ValueError:
        return 0


@router.get("/search", response_class=HTMLResponse, response_model=None)
async def search_page(request: Request, session: SessionDep, params: ParamsDep) -> Response:
    error: dict[str, object] | None = None
    page = None
    items = []
    warnings = []
    try:
        parsed = await resolve(session, params)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_400_BAD_REQUEST:
            raise
        error = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        flt = SearchFilter(sort=params.sort, seed=params.seed)
    else:
        flt = parsed.filter
        warnings = parsed.warnings
        from_panel = params.has_fields() or "panel" in request.query_params
        if from_panel or (flt.sort == "random" and flt.seed is None):
            flt = flt.with_seed()
            return RedirectResponse(
                search_url(format_query(flt), flt.sort, flt.seed), status.HTTP_303_SEE_OTHER
            )
        try:
            page = await search(session, flt, params.cursor, params.limit)
        except CursorError as exc:
            error = {"message": str(exc)}
        else:
            items = await result_items(session, page)
    query = params.q if params.q is not None else ""
    canonical = format_query(flt) if error is None else query
    start = _start(request.query_params.get("start"))
    return render(
        request,
        "search.html",
        q=query,
        canonical=canonical,
        flt=flt,
        fields=panel_fields(flt),
        error=error,
        warnings=warnings,
        page=page,
        items=items,
        start=start,
        next_url=(
            search_url(
                canonical,
                flt.sort,
                flt.seed,
                cursor=page.next_cursor,
                start=start + len(page.files),
            )
            if page and page.next_cursor
            else None
        ),
        first_url=search_url(canonical, flt.sort, flt.seed) if params.cursor else None,
        folders=await folder_choices(session),
        saved=await list_saved(session),
        sorts=SORTS,
        sort_labels=SORT_LABELS,
        media_kinds=[k.value for k in MediaKind],
        status_words=list(STATUS_WORDS),
        search_url=search_url,
    )
