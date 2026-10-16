from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import database_ok, get_session
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
    NavItem("/upload", "Upload", 1),
    NavItem("/review", "Review", 3),
    NavItem("/library", "Library", 4),
    NavItem("/search", "Search", 7),
    NavItem("/tags", "Tags", 4),
    NavItem("/sources", "Sources", 5),
    NavItem("/jobs", "Jobs", 5),
    NavItem("/settings", "Settings", 8),
)
FILE_PAGE_PHASE = 4


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
    return render(request, "dashboard.html", db_ok=db_ok, jobs=jobs)


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
async def file_detail(request: Request, file_id: int) -> HTMLResponse:
    return render(request, "coming_soon.html", title=f"File {file_id}", phase=FILE_PAGE_PHASE)
