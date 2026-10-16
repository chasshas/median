"""Media streaming: originals (with HTTP Range, via FileResponse) and thumbnails."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.models_files import File
from app.storage import get_paths

router = APIRouter(prefix="/media", tags=["media"])


async def _file(session: AsyncSession, file_id: int) -> File:
    file = await session.get(File, file_id)
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    return file


@router.get("/{file_id}/original")
async def original(
    file_id: int, session: Annotated[AsyncSession, Depends(get_session)]
) -> FileResponse:
    file = await _file(session, file_id)
    path = get_paths().original_path(file.status.value, file.staging_path, file.sha_hex, file.ext)
    if path is None or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "original not available")
    return FileResponse(
        path,
        media_type=file.mime or "application/octet-stream",
        content_disposition_type="inline",
        filename=file.original_filename or path.name,
    )


@router.get("/{file_id}/thumb")
async def thumb(
    file_id: int, session: Annotated[AsyncSession, Depends(get_session)]
) -> FileResponse:
    file = await _file(session, file_id)
    path = get_paths().thumb_path(file.sha_hex)
    if not file.has_thumb or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no thumbnail")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "max-age=86400"})
