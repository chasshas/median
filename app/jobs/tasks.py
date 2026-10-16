"""Task registration. Each task delegates to a service; keep task bodies thin."""

import logging

from app.jobs.app import job_app

logger = logging.getLogger(__name__)


@job_app.task(name="ping", queue="default")
async def ping(message: str = "ping") -> str:
    logger.info("ping task received %r, replying pong", message)
    return "pong"
