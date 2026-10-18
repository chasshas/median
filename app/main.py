import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import router as api_router
from app.api.health import router as health_router
from app.config import Settings, get_settings
from app.crawl.registry import get_registry
from app.db.engine import create_engine, create_sessionmaker
from app.jobs import open_jobs
from app.ops.auth import AuthMiddleware, LoginLimiter, SessionKey
from app.ops.settings import load_settings
from app.web import STATIC_DIR
from app.web.library import router as library_router
from app.web.media import router as media_router
from app.web.ops import router as ops_router
from app.web.pages import router as pages_router
from app.web.review import router as review_router
from app.web.search import router as search_router
from app.web.sources import router as sources_router

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        if getattr(app.state, "registry", None) is None:
            app.state.registry = get_registry()  # add-on discovery at web startup
        if not settings.app_password:
            logger.warning(
                "APP_PASSWORD is not set: anyone who can reach this server can use Median."
                " Set APP_PASSWORD (and SECRET_KEY) before exposing it beyond a trusted LAN."
            )
        try:
            async with app.state.sessionmaker() as session:
                await load_settings(session)
        except Exception:
            logger.exception("could not load settings from the database; using the environment")
        try:
            async with open_jobs(settings.database_url):
                yield
        finally:
            await engine.dispose()

    app = FastAPI(title="Median", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_key = SessionKey(settings)
    app.state.login_limiter = LoginLimiter()
    app.add_middleware(AuthMiddleware, settings=settings, key=app.state.session_key)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(health_router)
    app.include_router(api_router)
    app.include_router(media_router)
    app.include_router(review_router)
    app.include_router(library_router)
    app.include_router(search_router)
    app.include_router(sources_router)
    app.include_router(ops_router)
    app.include_router(pages_router)
    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
