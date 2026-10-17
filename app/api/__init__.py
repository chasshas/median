from fastapi import APIRouter

from app.api.debug import router as debug_router
from app.api.file_tags import router as file_tags_router
from app.api.files import router as files_router
from app.api.folders import router as folders_router
from app.api.library import router as library_router
from app.api.review import router as review_router
from app.api.similar import router as similar_router
from app.api.tags import router as tags_router
from app.api.uploads import router as uploads_router

router = APIRouter(prefix="/api")
router.include_router(debug_router)
router.include_router(uploads_router)
router.include_router(files_router)
router.include_router(similar_router)
router.include_router(review_router)
router.include_router(file_tags_router)
router.include_router(folders_router)
router.include_router(tags_router)
router.include_router(library_router)
