"""The review page and its htmx/JS fragments (single, grid, compare)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.review import queue_filters
from app.db.engine import get_session
from app.db.models_files import File, FileSource, FileStatus, MediaKind
from app.dedup.views import similar_badges, similar_files
from app.ingest.views import file_detail, files_with_status, status_counts
from app.library.folders import folder_choices
from app.review.queue import QueueFilters
from app.storage import get_paths
from app.web.pages import render

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]
FAILED_LIMIT = 500
GRID_LIMIT = 500


def _original_available(file: File) -> bool:
    path = get_paths().original_path(file.status.value, file.staging_path, file.sha_hex, file.ext)
    return path is not None and path.is_file()


async def _source_keys(session: AsyncSession) -> list[str]:
    rows = await session.scalars(
        select(FileSource.source_key).distinct().order_by(FileSource.source_key)
    )
    return list(rows.all())


@router.get("/review", response_class=HTMLResponse)
async def review_page(
    request: Request,
    session: SessionDep,
    filters: Annotated[QueueFilters, Depends(queue_filters)],
    tab: Literal["pending", "failed"] = "pending",
) -> HTMLResponse:
    query = request.url.query
    return render(
        request,
        "review.html",
        tab=tab,
        filters=filters,
        query=query,
        counts=await status_counts(session),
        folders=await folder_choices(session),
        source_keys=await _source_keys(session),
        media_kinds=[k.value for k in MediaKind],
        errors=(
            await files_with_status(session, FileStatus.error, FAILED_LIMIT)
            if tab == "failed"
            else []
        ),
    )


@router.get("/review/fragments/single", response_class=HTMLResponse)
async def single_fragment(request: Request, session: SessionDep, file_id: int) -> HTMLResponse:
    """The current file large, with its details and the strip of similar files."""
    detail = await file_detail(session, file_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    similar = [s for s in await similar_files(session, file_id) if not s.dismissed]
    return render(
        request,
        "fragments/review_single.html",
        detail=detail,
        file=detail.file,
        similar=similar,
        available=_original_available(detail.file),
    )


@router.get("/review/fragments/grid", response_class=HTMLResponse)
async def grid_fragment(
    request: Request,
    session: SessionDep,
    ids: Annotated[str, Query(pattern=r"^\d+(,\d+)*$", max_length=10_000)],
) -> HTMLResponse:
    """Tiles for the given file ids, in that order (the page keeps the queue order)."""
    wanted = list(dict.fromkeys(int(i) for i in ids.split(",")))[:GRID_LIMIT]
    rows = await session.scalars(select(File).where(File.id.in_(wanted)))
    by_id = {f.id: f for f in rows.all()}
    files = [by_id[i] for i in wanted if i in by_id]
    return render(
        request,
        "fragments/review_grid.html",
        files=files,
        badges=await similar_badges(session, [f.id for f in files]),
    )


@router.get("/review/fragments/compare", response_class=HTMLResponse)
async def compare_fragment(
    request: Request, session: SessionDep, left: int, right: int
) -> HTMLResponse:
    """The current file (left) beside one similar file (right)."""
    files = {
        f.id: f
        for f in (await session.scalars(select(File).where(File.id.in_([left, right])))).all()
    }
    if left not in files or right not in files or left == right:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    pair = next((s for s in await similar_files(session, left) if s.other_id == right), None)
    lf, rf = files[left], files[right]
    lpx, rpx = (lf.width or 0) * (lf.height or 0), (rf.width or 0) * (rf.height or 0)
    larger = "left" if lpx > rpx else "right" if rpx > lpx else None
    return render(
        request,
        "fragments/review_compare.html",
        left=lf,
        right=rf,
        pair=pair,
        larger=larger,
        available={left: _original_available(lf), right: _original_available(rf)},
    )
