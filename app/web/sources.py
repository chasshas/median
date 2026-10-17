"""Sources and jobs pages (ARCHITECTURE.md section 13): forms generated from add-on schemas."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawl import service
from app.crawl.base import secret_fields
from app.crawl.forms import FormError, form_fields, parse_config_form
from app.crawl.registry import AddonInfo, Registry
from app.crawl.views import app_registry, log_tail, run_views, source_views
from app.db.engine import get_session
from app.db.models_crawl import DEFAULT_MAX_RUNTIME_MINUTES, CrawlSource
from app.library.folders import folder_choices
from app.library.tags import split_tag_input
from app.storage import get_paths
from app.web.pages import render

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]
RegistryDep = Annotated[Registry, Depends(app_registry)]
JOBS_PER_PAGE = 100


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/sources", response_class=HTMLResponse)
async def sources_page(
    request: Request, session: SessionDep, registry: RegistryDep
) -> HTMLResponse:
    views = await source_views(session, registry, await service.list_sources(session))
    return render(
        request,
        "sources.html",
        sources=views,
        addons=registry.sorted(),
        addon_errors=registry.errors,
        notice=request.query_params.get("notice"),
    )


def _values(source: CrawlSource | None) -> dict[str, Any]:
    if source is None:
        return {
            "name": "",
            "schedule": "",
            "enabled": True,
            "default_folder_id": None,
            "default_tags": "",
            "max_runtime_minutes": DEFAULT_MAX_RUNTIME_MINUTES,
        }
    return {
        "name": source.name,
        "schedule": source.schedule or "",
        "enabled": source.enabled,
        "default_folder_id": source.default_folder_id,
        "default_tags": ", ".join(source.default_tags),
        "max_runtime_minutes": source.max_runtime_minutes,
    }


async def _render_form(
    request: Request,
    session: AsyncSession,
    *,
    info: AddonInfo | None,
    addon_key: str,
    source: CrawlSource | None,
    values: dict[str, Any],
    config: dict[str, Any],
    errors: dict[str, str] | None = None,
    message: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    present: set[str] = set()
    fields = []
    if info is not None:
        names = secret_fields(info.config_model)
        present = service.present_secrets(get_paths(), source.id if source else None, names)
        fields = form_fields(info.config_model, config, present)
    response = render(
        request,
        "source_form.html",
        info=info,
        addon_key=addon_key,
        source=source,
        values=values,
        fields=fields,
        folders=await folder_choices(session),
        errors=errors or {},
        message=message,
    )
    response.status_code = status_code
    return response


@router.get("/sources/new", response_class=HTMLResponse)
async def new_source(
    request: Request, session: SessionDep, registry: RegistryDep, addon: str
) -> HTMLResponse:
    info = registry.get(addon)
    if info is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"add-on {addon!r} is not loaded")
    return await _render_form(
        request, session, info=info, addon_key=addon, source=None, values=_values(None), config={}
    )


@router.get("/sources/{source_id}/edit", response_class=HTMLResponse)
async def edit_source(
    request: Request, session: SessionDep, registry: RegistryDep, source_id: int
) -> HTMLResponse:
    source = await _source(session, source_id)
    return await _render_form(
        request,
        session,
        info=registry.get(source.addon_key),
        addon_key=source.addon_key,
        source=source,
        values=_values(source),
        config=source.config,
    )


async def _source(session: AsyncSession, source_id: int) -> CrawlSource:
    try:
        return await service.get_source(session, source_id)
    except service.SourceNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found") from exc


def _int_or_none(raw: object) -> int | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def _submit(
    request: Request,
    session: AsyncSession,
    registry: Registry,
    addon_key: str,
    source: CrawlSource | None,
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    info = registry.get(addon_key)
    runtime = _int_or_none(form.get("max_runtime_minutes")) or DEFAULT_MAX_RUNTIME_MINUTES
    values = {
        "name": str(form.get("name") or ""),
        "schedule": str(form.get("schedule") or ""),
        "enabled": form.get("enabled") in ("on", "true", "1"),
        "default_folder_id": _int_or_none(form.get("default_folder_id")),
        "default_tags": str(form.get("default_tags") or ""),
        "max_runtime_minutes": runtime,
    }
    config: dict[str, Any] = source.config if source and info is None else {}
    secrets: dict[str, bytes | None] = {}
    try:
        if info is not None:
            parsed = await parse_config_form(info.config_model, form)
            config, secrets = parsed.config, parsed.secrets
        data = service.SourceInput(
            name=values["name"],
            addon_key=addon_key,
            config=config,
            schedule=values["schedule"],
            enabled=values["enabled"],
            default_folder_id=values["default_folder_id"],
            default_tags=split_tag_input(values["default_tags"]),
            max_runtime_minutes=runtime,
        )
        if source is None:
            created = await service.create_source(session, registry, get_paths(), data, secrets)
            return _see_other(f"/sources?notice=created-{created.id}")
        await service.update_source(session, registry, get_paths(), source.id, data, secrets)
        return _see_other(f"/sources?notice=saved-{source.id}")
    except (FormError, service.SourceError) as exc:
        await session.rollback()
        if source is not None:
            await session.refresh(source)
        return await _render_form(
            request,
            session,
            info=info,
            addon_key=addon_key,
            source=source,
            values=values,
            config=config,
            errors=exc.fields,
            message=str(exc),
            status_code=status.HTTP_400_BAD_REQUEST,
        )


@router.post("/sources/new", response_model=None)
async def create_source(
    request: Request, session: SessionDep, registry: RegistryDep, addon: str
) -> HTMLResponse | RedirectResponse:
    if registry.get(addon) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"add-on {addon!r} is not loaded")
    return await _submit(request, session, registry, addon, None)


@router.post("/sources/{source_id}/edit", response_model=None)
async def update_source(
    request: Request, session: SessionDep, registry: RegistryDep, source_id: int
) -> HTMLResponse | RedirectResponse:
    source = await _source(session, source_id)
    return await _submit(request, session, registry, source.addon_key, source)


@router.post("/sources/{source_id}/toggle")
async def toggle_source(session: SessionDep, source_id: int) -> RedirectResponse:
    source = await _source(session, source_id)
    await service.set_enabled(session, source_id, not source.enabled)
    return _see_other("/sources")


@router.post("/sources/{source_id}/run")
async def run_source(session: SessionDep, source_id: int) -> RedirectResponse:
    await _source(session, source_id)
    try:
        run = await service.queue_run(session, source_id)
    except service.RunActive as exc:
        if exc.run_id:
            return _see_other(f"/jobs/{exc.run_id}?notice=already-active")
        return _see_other("/sources?notice=already-active")
    return _see_other(f"/jobs/{run.id}")


@router.post("/sources/{source_id}/delete")
async def delete_source(session: SessionDep, source_id: int) -> RedirectResponse:
    try:
        await service.delete_source(session, get_paths(), source_id)
    except service.SourceNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found") from exc
    except service.RunActive:
        return _see_other("/sources?notice=delete-active")
    return _see_other("/sources?notice=deleted")


# Jobs --------------------------------------------------------------------------------------


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(
    request: Request, session: SessionDep, source_id: int | None = None
) -> HTMLResponse:
    runs = await service.list_runs(session, source_id=source_id, limit=JOBS_PER_PAGE)
    views = await run_views(session, runs)
    return render(
        request,
        "jobs.html",
        runs=views,
        source_id=source_id,
        any_active=any(v.run.is_active for v in views),
    )


async def _run_view(session: AsyncSession, run_id: int):
    try:
        run = await service.get_run(session, run_id)
    except service.RunNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from exc
    (view,) = await run_views(session, [run])
    return view


@router.get("/jobs/{run_id}", response_class=HTMLResponse)
async def job_page(request: Request, session: SessionDep, run_id: int) -> HTMLResponse:
    view = await _run_view(session, run_id)
    return render(
        request,
        "job.html",
        view=view,
        run=view.run,
        log=log_tail(get_paths(), view.run),
        notice=request.query_params.get("notice"),
    )


@router.get("/jobs/{run_id}/fragments/live", response_class=HTMLResponse)
async def job_live(request: Request, session: SessionDep, run_id: int) -> HTMLResponse:
    """Status, stats and log tail; polls itself every 2 s while the run is active."""
    view = await _run_view(session, run_id)
    return render(
        request,
        "fragments/job_live.html",
        view=view,
        run=view.run,
        log=log_tail(get_paths(), view.run),
    )


@router.post("/jobs/{run_id}/cancel")
async def cancel_job(session: SessionDep, run_id: int) -> RedirectResponse:
    try:
        await service.cancel_run(session, run_id)
    except service.RunNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from exc
    except service.RunNotActive:
        return _see_other(f"/jobs/{run_id}?notice=not-active")
    return _see_other(f"/jobs/{run_id}?notice=cancel-requested")
