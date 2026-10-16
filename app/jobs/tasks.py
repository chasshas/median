"""Task registration. Each task delegates to a service; keep task bodies thin."""

import logging
from typing import Any

from app.config import get_settings
from app.db.engine import job_sessionmaker
from app.jobs.app import job_app
from app.storage import get_paths

logger = logging.getLogger(__name__)


@job_app.task(name="ping", queue="default")
async def ping(message: str = "ping") -> str:
    logger.info("ping task received %r, replying pong", message)
    return "pong"


@job_app.task(name="process_ingest", queue="ingest")
async def process_ingest(
    staging_path: str, origin: dict[str, Any], original_filename: str, batch_id: str
) -> str:
    from app.ingest import process
    from app.ingest.origin import IngestOrigin

    async with job_sessionmaker(get_settings().database_url) as sessionmaker:
        outcome = await process.process_ingest(
            sessionmaker,
            get_paths(),
            staging_path,
            IngestOrigin.model_validate(origin),
            original_filename,
            batch_id,
        )
    return outcome.value


@job_app.task(name="retry_file", queue="ingest")
async def retry_file(file_id: int) -> str:
    from app.ingest import process

    async with job_sessionmaker(get_settings().database_url) as sessionmaker:
        outcome = await process.retry_file(sessionmaker, get_paths(), file_id)
    return outcome.value
