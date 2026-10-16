from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.db.engine import database_ok

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    db_ok = await database_ok(request.app.state.engine)
    body = {"status": "ok" if db_ok else "error", "database": "ok" if db_ok else "unreachable"}
    return JSONResponse(body, status_code=200 if db_ok else 503)
