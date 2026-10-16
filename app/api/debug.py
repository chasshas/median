from fastapi import APIRouter, status
from pydantic import BaseModel

from app.jobs import defer_ping

router = APIRouter(prefix="/debug", tags=["debug"])


class PingDeferred(BaseModel):
    job_id: int
    task: str = "ping"


@router.post("/ping", status_code=status.HTTP_202_ACCEPTED)
async def ping() -> PingDeferred:
    job_id = await defer_ping("ping from /api/debug/ping")
    return PingDeferred(job_id=job_id)
