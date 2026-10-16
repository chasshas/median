"""Progress of an upload batch or crawl run, from ingest_events."""

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_files import IngestEvent, IngestOutcome
from app.jobs import pending_ingest_jobs


@dataclass
class BatchStatus:
    batch_id: str
    counts: dict[str, int] = field(default_factory=dict)
    pending: int = 0
    files: list[tuple[int, str]] = field(default_factory=list)  # (file_id, outcome)

    @property
    def done(self) -> bool:
        return self.pending == 0

    @property
    def total(self) -> int:
        return sum(self.counts.values())


async def batch_status(session: AsyncSession, batch_id: str) -> BatchStatus:
    rows = await session.execute(
        select(IngestEvent.outcome, func.count())
        .where(IngestEvent.batch_id == batch_id)
        .group_by(IngestEvent.outcome)
    )
    found = {outcome.value: count for outcome, count in rows.all()}
    counts = {outcome.value: found.get(outcome.value, 0) for outcome in IngestOutcome}
    files = await session.execute(
        select(IngestEvent.file_id, IngestEvent.outcome)
        .where(IngestEvent.batch_id == batch_id, IngestEvent.file_id.is_not(None))
        .order_by(IngestEvent.id)
    )
    return BatchStatus(
        batch_id,
        counts,
        await pending_ingest_jobs(session, batch_id),
        [(file_id, outcome.value) for file_id, outcome in files.all()],
    )
