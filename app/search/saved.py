"""Saved searches: a name and a query string (ARCHITECTURE.md section 6)."""

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_search import SavedSearch
from app.search.query import parse

MAX_NAME = 100


class SavedSearchError(ValueError):
    pass


class SavedSearchExists(SavedSearchError):
    pass


class SavedSearchNotFound(LookupError):
    pass


def as_json(saved: SavedSearch) -> dict[str, object]:
    return {
        "id": saved.id,
        "name": saved.name,
        "query": saved.query,
        "created_at": saved.created_at.isoformat() if saved.created_at else None,
    }


def _name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        raise SavedSearchError("a saved search needs a name")
    if len(name) > MAX_NAME:
        raise SavedSearchError(f"a name can have at most {MAX_NAME} characters")
    return name


async def list_saved(session: AsyncSession) -> list[SavedSearch]:
    rows = await session.scalars(
        select(SavedSearch).order_by(func.lower(SavedSearch.name), SavedSearch.id)
    )
    return list(rows.all())


async def get_saved(session: AsyncSession, saved_id: int) -> SavedSearch:
    saved = await session.get(SavedSearch, saved_id)
    if saved is None:
        raise SavedSearchNotFound(f"saved search {saved_id} does not exist")
    return saved


async def _flush(session: AsyncSession, name: str) -> None:
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        raise SavedSearchExists(f'a saved search named "{name}" already exists') from exc


async def create_saved(session: AsyncSession, name: str, query: str) -> SavedSearch:
    """Store a query; it must parse (SearchSyntaxError otherwise)."""
    name = _name(name)
    await parse(session, query)
    saved = SavedSearch(name=name, query=query.strip())
    session.add(saved)
    await _flush(session, name)
    await session.refresh(saved)
    return saved


async def update_saved(
    session: AsyncSession, saved_id: int, name: str | None = None, query: str | None = None
) -> SavedSearch:
    saved = await get_saved(session, saved_id)
    if name is not None:
        saved.name = _name(name)
    if query is not None:
        await parse(session, query)
        saved.query = query.strip()
    await _flush(session, saved.name)
    return saved


async def delete_saved(session: AsyncSession, saved_id: int) -> None:
    await session.delete(await get_saved(session, saved_id))
    await session.flush()
