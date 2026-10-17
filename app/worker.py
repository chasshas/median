"""Worker entry point: make sure the crawler tools are installed, then run the job worker.

python -m app.worker [extra procrastinate worker options]
"""

import logging
import os
import sys

from app.crawl.tools import ensure_tools

WORKER_ARGS = ["procrastinate", "--app=app.jobs.app.job_app", "--log-level=info", "worker"]


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if os.environ.get("MEDIAN_SKIP_TOOLS") != "1":
        ensure_tools()
    args = [*WORKER_ARGS, *(sys.argv[1:] if argv is None else argv)]
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp(args[0], args)  # noqa: S606 - fixed program, argument list


if __name__ == "__main__":
    main()
