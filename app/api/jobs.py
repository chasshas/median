"""Job runs (JSON): list with stats, detail with the log tail, cancel."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawl import service
from app.crawl.views import log_tail, run_views
from app.db.engine import get_session
from app.db.models_crawl import JobKind, JobStatus
from app.storage import get_paths

router = APIRouter(prefix="/jobs", tags=["jobs"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def _run(session: AsyncSession, run_id: int):
    try:
        return await service.get_run(session, run_id)
    except service.RunNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from exc


@router.get("")
async def list_runs(
    session: SessionDep,
    source_id: int | None = None,
    kind: Annotated[list[JobKind] | None, Query()] = None,
    status_: Annotated[list[JobStatus] | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    runs = await service.list_runs(
        session, source_id=source_id, kinds=kind, statuses=status_, limit=limit, offset=offset
    )
    return {"runs": [v.as_json() for v in await run_views(session, runs)]}


@router.get("/{run_id}")
async def get_run(run_id: int, session: SessionDep) -> dict[str, object]:
    (view,) = await run_views(session, [await _run(session, run_id)])
    return view.as_json()


@router.get("/{run_id}/log", response_class=PlainTextResponse)
async def get_log(run_id: int, session: SessionDep) -> str:
    return log_tail(get_paths(), await _run(session, run_id))


@router.post("/{run_id}/cancel")
async def cancel(run_id: int, session: SessionDep) -> dict[str, object]:
    """Cancel a queued run, or ask a running one to stop."""
    try:
        run = await service.cancel_run(session, run_id)
    except service.RunNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from exc
    except service.RunNotActive as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    (view,) = await run_views(session, [run])
    return view.as_json()
