"""Library, tags and file pages (ARCHITECTURE.md section 13), plus their htmx/JS fragments."""

from dataclasses import replace
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.library import ContextDep
from app.db.engine import get_session
from app.db.models_files import FileStatus
from app.dedup.views import similar_files
from app.ingest.views import file_detail
from app.library import folders, tags
from app.library.browse import PER_PAGE, SORTS, LibraryContext, library_page, neighbors
from app.web.pages import render

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _ancestors(nodes: list[folders.FolderNode], folder_id: int | None) -> set[int]:
    """Ids of the folder and every folder above it (to open the tree down to it)."""
    parents = {n.id: n.parent_id for n in folders.flatten(nodes)}
    out: set[int] = set()
    current = folder_id
    while current is not None and current in parents and current not in out:
        out.add(current)
        current = parents[current]
    return out


@router.get("/library", response_class=HTMLResponse)
async def library(
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    tree = await folders.get_tree(session)
    flat = folders.flatten(tree)
    selected = next((n for n in flat if n.id == ctx.folder_id), None)
    if ctx.folder_id is not None and selected is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "folder not found")
    result = await library_page(session, ctx, page, PER_PAGE)
    subtree = set(await folders.subtree_ids(session, selected.id)) if selected else set()
    return render(
        request,
        "library.html",
        ctx=ctx,
        tree=tree,
        flat=flat,
        selected=selected,
        open_ids=_ancestors(tree, ctx.folder_id),
        move_targets=[n for n in flat if n.id not in subtree],
        result=result,
        sorts=SORTS,
        all_count=sum(n.subtree_count for n in tree),
        lib_url=partial(library_url, ctx),
        file_query=ctx.query_string(**{"from": "library"}),
    )


def library_url(ctx: LibraryContext, page: int | None = None, **changes: object) -> str:
    """The library URL for ctx with some fields changed (folder_id, include_subfolders, sort,
    direction) and an optional page."""
    query = replace(ctx, **changes).query_string(page=page if page and page > 1 else None)  # type: ignore[arg-type]
    return f"/library?{query}" if query else "/library"


@router.get("/tags", response_class=HTMLResponse)
async def tags_page(request: Request, session: SessionDep, q: str | None = None) -> HTMLResponse:
    return render(request, "tags.html", tags=await tags.list_tags(session, q), q=q or "")


async def _file_tags_context(session: AsyncSession, file_id: int) -> dict[str, object]:
    rows = await tags.file_tags(session, file_id)
    return {
        "active_tags": [r for r in rows if not r.removed],
        "removed_tags": [r for r in rows if r.removed],
    }


@router.get("/files/{file_id}", response_class=HTMLResponse)
async def file_page(
    request: Request,
    file_id: int,
    session: SessionDep,
    ctx: ContextDep,
    from_: Annotated[str | None, Query(alias="from")] = None,
) -> HTMLResponse:
    """The file with its details, folder, tags and similar files. Opened from the library
    (from=library plus the grid's folder, sub, sort and dir), it gets prev/next links."""
    detail = await file_detail(session, file_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    file_nav = await neighbors(session, ctx, file_id) if from_ == "library" else None
    file = detail.file
    return render(
        request,
        "file.html",
        detail=detail,
        file=file,
        similar=await similar_files(session, file_id),
        folder_path=(
            await folders.path_of(session, file.folder_id) if file.folder_id is not None else None
        ),
        folders=(
            await folders.folder_choices(session) if file.status is FileStatus.archived else []
        ),
        ctx=ctx,
        file_nav=file_nav,
        file_query=ctx.query_string(**{"from": "library"}),
        back_url=library_url(ctx) if from_ == "library" else None,
        **await _file_tags_context(session, file_id),
    )


@router.get("/files/{file_id}/fragments/tags", response_class=HTMLResponse)
async def file_tags_fragment(request: Request, file_id: int, session: SessionDep) -> HTMLResponse:
    """The tag editor of the file page (re-rendered after each change)."""
    if await file_detail(session, file_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    return render(
        request,
        "fragments/file_tags.html",
        file_id=file_id,
        **await _file_tags_context(session, file_id),
    )
