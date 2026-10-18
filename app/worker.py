"""Worker entry point: recover from the previous worker's stop, make sure the crawler tools are
installed, then run the job worker.

python -m app.worker [extra procrastinate worker options]
"""

import asyncio
import json
import logging
import os
import sys

from app.config import get_settings
from app.crawl.tools import ensure_tools

WORKER_ARGS = ["procrastinate", "--app=app.jobs.app.job_app", "--log-level=info", "worker"]

logger = logging.getLogger("app.worker")


async def recover() -> dict[str, list[int]]:
    """Runs and queue jobs left running become failed ("interrupted"); stuck files re-queued."""
    from app.db.engine import job_sessionmaker
    from app.jobs import open_jobs
    from app.ops.housekeeping import recover_on_startup

    database_url = get_settings().database_url
    async with open_jobs(database_url), job_sessionmaker(database_url) as sessionmaker:
        result = await recover_on_startup(sessionmaker)
    return result.as_dict()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        logger.info("startup recovery: %s", json.dumps(asyncio.run(recover())))
    except Exception:
        logger.exception("startup recovery failed; starting the worker anyway")
    if os.environ.get("MEDIAN_SKIP_TOOLS") != "1":
        ensure_tools()
    args = [*WORKER_ARGS, *(sys.argv[1:] if argv is None else argv)]
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp(args[0], args)  # noqa: S606 - fixed program, argument list


if __name__ == "__main__":
    main()
