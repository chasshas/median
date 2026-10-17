"""Crawl sources (JSON): list, create, edit, enable, delete, run now; add-ons and load errors."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawl import service
from app.crawl.forms import form_fields
from app.crawl.registry import Registry
from app.crawl.views import app_registry, source_views
from app.db.engine import get_session
from app.db.models_crawl import DEFAULT_MAX_RUNTIME_MINUTES
from app.storage import get_paths

router = APIRouter(prefix="/sources", tags=["sources"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
RegistryDep = Annotated[Registry, Depends(app_registry)]


class SourceBody(BaseModel):
    name: str
    addon_key: str = ""  # required on create; a source's add-on never changes
    config: dict[str, Any] = {}
    schedule: str | None = None
    enabled: bool = True
    default_folder_id: int | None = None
    default_tags: list[str] = []
    max_runtime_minutes: Annotated[int, Field(ge=1)] = DEFAULT_MAX_RUNTIME_MINUTES

    def to_input(self) -> service.SourceInput:
        return service.SourceInput(**self.model_dump())


class EnabledBody(BaseModel):
    enabled: bool


def _bad(exc: service.SourceError) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, {"message": str(exc), "fields": exc.fields})


async def _one(session: AsyncSession, registry: Registry, source_id: int) -> dict[str, object]:
    try:
        source = await service.get_source(session, source_id)
    except service.SourceNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found") from exc
    (view,) = await source_views(session, registry, [source])
    return view.as_json()


@router.get("")
async def list_sources(session: SessionDep, registry: RegistryDep) -> dict[str, object]:
    views = await source_views(session, registry, await service.list_sources(session))
    return {
        "sources": [v.as_json() for v in views],
        "addons": [
            {
                "key": a.key,
                "name": a.name,
                "version": a.version,
                "description": a.description,
                "origin": a.origin,
                "fields": [f.__dict__ for f in form_fields(a.config_model)],
            }
            for a in registry.sorted()
        ],
        "addon_errors": [e.__dict__ for e in registry.errors],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(body: SourceBody, session: SessionDep, registry: RegistryDep) -> dict[str, object]:
    try:
        source = await service.create_source(session, registry, get_paths(), body.to_input())
    except service.SourceError as exc:
        raise _bad(exc) from exc
    return await _one(session, registry, source.id)


@router.get("/{source_id}")
async def get(source_id: int, session: SessionDep, registry: RegistryDep) -> dict[str, object]:
    return await _one(session, registry, source_id)


@router.put("/{source_id}")
async def replace(
    source_id: int, body: SourceBody, session: SessionDep, registry: RegistryDep
) -> dict[str, object]:
    try:
        await service.update_source(session, registry, get_paths(), source_id, body.to_input())
    except service.SourceNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found") from exc
    except service.SourceError as exc:
        raise _bad(exc) from exc
    return await _one(session, registry, source_id)


@router.patch("/{source_id}")
async def set_enabled(
    source_id: int, body: EnabledBody, session: SessionDep, registry: RegistryDep
) -> dict[str, object]:
    try:
        await service.set_enabled(session, source_id, body.enabled)
    except service.SourceNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found") from exc
    return await _one(session, registry, source_id)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(source_id: int, session: SessionDep) -> Response:
    try:
        await service.delete_source(session, get_paths(), source_id)
    except service.SourceNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found") from exc
    except service.RunActive as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{source_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_now(source_id: int, session: SessionDep) -> dict[str, object]:
    """Queue a run now (refused with 409 while one is queued or running)."""
    try:
        run = await service.queue_run(session, source_id)
    except service.SourceNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found") from exc
    except service.RunActive as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"message": str(exc), "run_id": exc.run_id}
        ) from exc
    return {"run_id": run.id, "status": run.status.value}
