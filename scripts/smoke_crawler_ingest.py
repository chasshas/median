"""Ingest one file as a crawler add-on would, for scripts/smoke.sh (phase 4).

python scripts/smoke_crawler_ingest.py <file> <source_key> <remote_id> <tag>[,<tag>...]
Copies the file into staging through the ingest service (tags with origin crawler) and prints
the batch id; the worker processes it. The source file is never moved or deleted.
"""

import asyncio
import sys
from pathlib import Path

from app.config import get_settings
from app.ingest.origin import IngestOrigin
from app.ingest.service import ingest
from app.jobs import open_jobs


async def main(path: Path, source_key: str, remote_id: str, tags: list[str]) -> None:
    origin = IngestOrigin(source_key=source_key, remote_id=remote_id, tags=tags)
    async with open_jobs(get_settings().database_url):
        ticket = await ingest(path, origin, copy=True)
    print(ticket.batch_id)


if __name__ == "__main__":
    file, key, remote, raw_tags = sys.argv[1:5]
    asyncio.run(main(Path(file), key, remote, [t for t in raw_tags.split(",") if t]))
