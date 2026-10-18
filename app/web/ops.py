"""Login, settings and maintenance actions (ARCHITECTURE.md section 13, phase 8)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import get_session
from app.db.models_crawl import JobKind
from app.ops import integrity
from app.ops.auth import (
    COOKIE_NAME,
    SESSION_SECONDS,
    LoginLimiter,
    make_token,
    password_matches,
    safe_next,
    token_valid,
)
from app.ops.backup import list_dumps
from app.ops.housekeeping import free_space
from app.ops.runs import MAINTENANCE_KINDS, MaintenanceActive, latest_run, queue_maintenance
from app.ops.schedule import next_runs, schedules
from app.ops.settings import (
    INFO,
    THRESHOLD_KEYS,
    SettingsError,
    env_defaults,
    load_settings,
    parse_form,
    save_settings,
)
from app.ops.tools_update import current_versions
from app.storage import get_paths
from app.web.pages import render

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]

RUN_LABELS: dict[JobKind, str] = {
    JobKind.backup: "Database backup",
    JobKind.integrity: "Integrity check",
    JobKind.update_tools: "Crawler tool update",
    JobKind.housekeeping: "Housekeeping (old runs, disk usage)",
    JobKind.purge_trash: "Purge trash",
    JobKind.recompute_similar: "Recompute similar pairs",
    JobKind.rehash: "Rehash (missing hashes only)",
}


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


def _limiter(request: Request) -> LoginLimiter:
    limiter = getattr(request.app.state, "login_limiter", None)
    if limiter is None:
        limiter = request.app.state.login_limiter = LoginLimiter()
    return limiter


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _session_key(request: Request) -> bytes:
    return request.app.state.session_key.value


# Login ---------------------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse, response_model=None)
async def login_page(request: Request, next: str = "/") -> HTMLResponse | RedirectResponse:
    password = request.app.state.settings.app_password
    if not password:
        return _see_other(safe_next(next))
    if token_valid(_session_key(request), password, request.cookies.get(COOKIE_NAME)):
        return _see_other(safe_next(next))
    return render(request, "login.html", next=safe_next(next), error=None)


@router.post("/login", response_model=None)
async def login(request: Request) -> HTMLResponse | RedirectResponse:
    password = request.app.state.settings.app_password
    form = await request.form()
    target = safe_next(str(form.get("next") or "/"))
    if not password:
        return _see_other(target)
    limiter = _limiter(request)
    ip = _client_ip(request)
    async with limiter.attempt(ip):
        ok = password_matches(password, str(form.get("password") or ""))
        if ok:
            limiter.succeeded(ip)
        else:
            limiter.failed(ip)
    if not ok:
        response = render(request, "login.html", next=target, error="Wrong password.")
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return response
    response = _see_other(target)
    response.set_cookie(
        COOKIE_NAME,
        make_token(_session_key(request), password),
        max_age=SESSION_SECONDS,
        path="/",
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


@router.post("/logout")
async def logout() -> RedirectResponse:
    response = _see_other("/login")
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax")
    return response


# Settings ------------------------------------------------------------------------------------


async def _maintenance_rows(session: AsyncSession, settings: Any) -> list[dict[str, Any]]:
    upcoming = await next_runs(session, settings)
    crons = schedules(settings)
    rows = []
    for kind in MAINTENANCE_KINDS:
        rows.append(
            {
                "kind": kind.value,
                "label": RUN_LABELS[kind],
                "schedule": crons.get(kind),
                "next": upcoming.get(kind.value),
                "last": await latest_run(session, kind),
            }
        )
    return rows


async def _settings_page(
    request: Request,
    session: AsyncSession,
    *,
    errors: dict[str, str] | None = None,
    submitted: dict[str, str] | None = None,
) -> HTMLResponse:
    settings = await load_settings(session)
    paths = get_paths()
    values = {k: str(v) for k, v in settings.values().items()}
    if submitted:
        values.update(submitted)
    response = render(
        request,
        "settings.html",
        info=INFO,
        values=values,
        defaults=env_defaults(),
        overridden=settings.overridden,
        errors=errors or {},
        notice=request.query_params.get("notice"),
        maintenance=await _maintenance_rows(session, settings),
        dumps=list_dumps(paths)[:50],
        versions=current_versions(paths),
        last_update=await latest_run(session, JobKind.update_tools),
        disk=free_space(paths),
        password_set=bool(request.app.state.settings.app_password),
    )
    if errors:
        response.status_code = status.HTTP_400_BAD_REQUEST
    return response


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session: SessionDep) -> HTMLResponse:
    return await _settings_page(request, session)


@router.post("/settings", response_model=None)
async def save_settings_form(
    request: Request, session: SessionDep
) -> HTMLResponse | RedirectResponse:
    form = {k: str(v) for k, v in (await request.form()).items()}
    try:
        changes = parse_form(form)
    except SettingsError as exc:
        return await _settings_page(request, session, errors=exc.errors, submitted=form)
    changed = await save_settings(session, changes)
    notice = "thresholds" if changed & THRESHOLD_KEYS else "saved"
    return _see_other(f"/settings?notice={notice}")


@router.post("/settings/run/{kind}")
async def run_now(request: Request, session: SessionDep, kind: str) -> RedirectResponse:
    try:
        job_kind = JobKind(kind)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown job") from exc
    if job_kind not in MAINTENANCE_KINDS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown job")
    form = await request.form()
    params: dict[str, Any] = {}
    if job_kind is JobKind.integrity and form.get("verify_hashes"):
        params["verify_hashes"] = True
    if job_kind is JobKind.rehash:
        params["missing_only"] = form.get("all") is None
    try:
        run = await queue_maintenance(session, job_kind, params)
    except MaintenanceActive as exc:
        if exc.run_id:
            return _see_other(f"/jobs/{exc.run_id}?notice=already-active")
        return _see_other("/settings?notice=already-active")
    return _see_other(f"/jobs/{run.id}")


# Integrity fixes -----------------------------------------------------------------------------


def _sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.sessionmaker


@router.post("/jobs/{run_id}/fix/{action}")
async def integrity_fix(request: Request, run_id: int, action: str) -> RedirectResponse:
    if action not in integrity.FIXES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown fix")
    try:
        await integrity.apply_fix(_sessionmaker(request), get_paths(), run_id, action)
    except LookupError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "no finished integrity check with this id"
        ) from exc
    return _see_other(f"/jobs/{run_id}?notice=fixed-{action}#fixes")
