from fastapi import APIRouter

from app.api.debug import router as debug_router
from app.api.files import router as files_router
from app.api.similar import router as similar_router
from app.api.uploads import router as uploads_router

router = APIRouter(prefix="/api")
router.include_router(debug_router)
router.include_router(uploads_router)
router.include_router(files_router)
router.include_router(similar_router)
