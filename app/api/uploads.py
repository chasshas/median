from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.multipart import MultipartError, stream_multipart
from app.db.engine import get_session
from app.ingest.batches import batch_status
from app.ingest.origin import IngestOrigin
from app.ingest.service import ingest
from app.library.tags import split_tag_input
from app.storage import get_paths
from app.storage.paths import is_valid_batch_id, new_upload_batch_id
from app.web import templates

router = APIRouter(prefix="/uploads", tags=["uploads"])

TRUTHY = {"1", "true", "on", "yes"}


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_upload(request: Request) -> JSONResponse:
    """Multipart form: files (any number), tags (comma separated), force (checkbox).

    Each file part is streamed to staging/<batch_id>/ as it arrives, then ingested.
    """
    paths = get_paths()
    batch_id = new_upload_batch_id()
    try:
        form = await stream_multipart(
            request.headers.get("content-type", ""),
            request.stream(),
            paths.staging_batch(batch_id),
        )
    except MultipartError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if not form.files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no files in the upload")
    origin = IngestOrigin(
        tags=split_tag_input(form.first("tags")),
        tag_origin="user",
        force=form.first("force").strip().lower() in TRUTHY,
    )
    try:
        for received in form.files:
            await ingest(
                received.path,
                origin,
                batch_id=batch_id,
                original_filename=received.filename,
                paths=paths,
            )
    except Exception:
        form.cleanup()  # parts not yet moved into place
        raise
    return JSONResponse(
        {"batch_id": batch_id, "files": len(form.files)}, status_code=status.HTTP_202_ACCEPTED
    )


@router.get("/{batch_id}", response_model=None)
async def get_upload(
    request: Request, batch_id: str, session: Annotated[AsyncSession, Depends(get_session)]
) -> Response:
    """Counts per outcome. htmx requests get an HTML fragment that polls until done."""
    if not is_valid_batch_id(batch_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown batch")
    batch = await batch_status(session, batch_id)
    if request.headers.get("hx-request") == "true":
        response: HTMLResponse = templates.TemplateResponse(
            request, "fragments/batch_status.html", {"batch": batch}
        )
        return response
    return JSONResponse(
        {
            "batch_id": batch.batch_id,
            "counts": batch.counts,
            "pending": batch.pending,
            "done": batch.done,
            "files": [{"file_id": f, "outcome": o} for f, o in batch.files],
        }
    )
