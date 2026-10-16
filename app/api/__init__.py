from fastapi import APIRouter

from app.api.debug import router as debug_router

router = APIRouter(prefix="/api")
router.include_router(debug_router)
