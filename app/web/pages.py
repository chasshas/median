from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import database_ok, get_session
from app.db.models_files import FileStatus
from app.dedup.views import similar_badges, similar_files
from app.ingest.views import file_detail, files_with_status, status_counts
from app.jobs import job_counts
from app.web import templates

router = APIRouter()


@dataclass(frozen=True)
class NavItem:
    path: str
    label: str
    phase: int | None  # phase that builds the page; None = already built


# Pages from ARCHITECTURE.md section 13. /files/<id> is reached from the library, not the nav.
NAV: tuple[NavItem, ...] = (
    NavItem("/", "Dashboard", None),
    NavItem("/upload", "Upload", None),
    NavItem("/review", "Review", None),  # simple grid until phase 3 builds the review flow
    NavItem("/library", "Library", 4),
    NavItem("/search", "Search", 7),
    NavItem("/tags", "Tags", 4),
    NavItem("/sources", "Sources", 5),
    NavItem("/jobs", "Jobs", 5),
    NavItem("/settings", "Settings", 8),
)
REVIEW_GRID_LIMIT = 500


def render(request: Request, template: str, **context: object) -> HTMLResponse:
    return templates.TemplateResponse(
        request, template, {"nav": NAV, "current": request.url.path, **context}
    )


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> HTMLResponse:
    db_ok = await database_ok(request.app.state.engine)
    jobs = await job_counts(session) if db_ok else {}
    files = await status_counts(session) if db_ok else {}
    return render(request, "dashboard.html", db_ok=db_ok, jobs=jobs, files=files)


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request) -> HTMLResponse:
    return render(request, "upload.html")


@router.get("/review", response_class=HTMLResponse)
async def review_page(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> HTMLResponse:
    """Temporary grid of pending files; phase 3 replaces it with the review flow."""
    pending = await files_with_status(session, FileStatus.pending_review, REVIEW_GRID_LIMIT)
    return render(
        request,
        "review.html",
        pending=pending,
        badges=await similar_badges(session, [f.id for f in pending]),
        processing=await files_with_status(session, FileStatus.processing, REVIEW_GRID_LIMIT),
        errors=await files_with_status(session, FileStatus.error, REVIEW_GRID_LIMIT),
        counts=await status_counts(session),
    )


def _coming_soon(item: NavItem) -> Callable[[Request], Awaitable[HTMLResponse]]:
    async def page(request: Request) -> HTMLResponse:
        return render(request, "coming_soon.html", title=item.label, phase=item.phase)

    page.__name__ = f"page_{item.path.strip('/')}"
    return page


for _item in NAV:
    if _item.phase is not None:
        router.add_api_route(
            _item.path, _coming_soon(_item), methods=["GET"], response_class=HTMLResponse
        )


@router.get("/files/{file_id}", response_class=HTMLResponse)
async def file_page(
    request: Request, file_id: int, session: Annotated[AsyncSession, Depends(get_session)]
) -> HTMLResponse:
    detail = await file_detail(session, file_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    return render(
        request,
        "file.html",
        detail=detail,
        file=detail.file,
        similar=await similar_files(session, file_id),
    )
