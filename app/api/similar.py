from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.models_dedup import SimilarMethod
from app.db.models_files import File
from app.dedup.service import dismiss_pair
from app.dedup.views import similar_files

router = APIRouter(tags=["similar"])


@router.get("/files/{file_id}/similar")
async def file_similar(
    file_id: int, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, object]:
    """Files perceptually similar to file_id, in any status, dismissed pairs included."""
    if await session.get(File, file_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    return {
        "file_id": file_id,
        "similar": [item.as_json() for item in await similar_files(session, file_id)],
    }


@router.post("/similar/{file_a}/{file_b}/{method}/dismiss")
async def dismiss(
    file_a: int,
    file_b: int,
    method: SimilarMethod,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    """Hide a similar pair for good (the ids may come in either order)."""
    if not await dismiss_pair(session, file_a, file_b, method):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "similar pair not found")
    await session.commit()
    a, b = sorted((file_a, file_b))
    return {"file_a_id": a, "file_b_id": b, "method": method.value, "dismissed": True}
