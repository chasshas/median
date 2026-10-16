from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.ingest.process import RetryError, prepare_retry
from app.jobs import defer_retry_file

router = APIRouter(prefix="/files", tags=["files"])


@router.post("/{file_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry(
    file_id: int, session: Annotated[AsyncSession, Depends(get_session)]
) -> JSONResponse:
    """Re-run processing for a file in status error."""
    try:
        await prepare_retry(session, file_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found") from exc
    except RetryError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await session.commit()
    job_id = await defer_retry_file(file_id)
    return JSONResponse(
        {"file_id": file_id, "job_id": job_id, "status": "processing"},
        status_code=status.HTTP_202_ACCEPTED,
    )
