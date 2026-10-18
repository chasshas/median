from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import database_ok, get_session
from app.db.models_crawl import JobKind, JobStatus
from app.ingest.views import status_counts
from app.jobs import job_counts
from app.ops.housekeeping import free_space
from app.ops.runs import latest_run
from app.storage import get_paths
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
    NavItem("/review", "Review", None),
    NavItem("/library", "Library", None),
    NavItem("/search", "Search", None),
    NavItem("/tags", "Tags", None),
    NavItem("/sources", "Sources", None),
    NavItem("/jobs", "Jobs", None),
    NavItem("/settings", "Settings", None),
)


def render(request: Request, template: str, **context: object) -> HTMLResponse:
    return templates.TemplateResponse(
        request, template, {"nav": NAV, "current": request.url.path, **context}
    )


def _parse_time(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> HTMLResponse:
    db_ok = await database_ok(request.app.state.engine)
    jobs = await job_counts(session) if db_ok else {}
    files = await status_counts(session) if db_ok else {}
    measured = None
    if db_ok:
        last = await latest_run(session, JobKind.housekeeping, JobStatus.succeeded)
        measured = (last.stats or {}).get("disk") if last is not None else None
    return render(
        request,
        "dashboard.html",
        db_ok=db_ok,
        jobs=jobs,
        files=files,
        disk=free_space(get_paths()),
        areas=(measured or {}).get("areas"),
        measured_at=_parse_time((measured or {}).get("measured_at")),
    )


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request) -> HTMLResponse:
    return render(request, "upload.html")


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
