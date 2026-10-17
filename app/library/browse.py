"""The library grid: archived files of a folder, sorted and paginated, and prev/next navigation
within the same context for the file page."""

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlencode

from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_files import File, FileStatus
from app.library.folders import subtree_ids

LibrarySort = Literal["archived", "collected", "size", "name"]
Direction = Literal["asc", "desc"]
SORTS: tuple[LibrarySort, ...] = ("archived", "collected", "size", "name")
DEFAULT_DIRECTION: dict[LibrarySort, Direction] = {
    "archived": "desc",
    "collected": "desc",
    "size": "desc",
    "name": "asc",
}
PER_PAGE = 60
MAX_PER_PAGE = 500


@dataclass(frozen=True)
class LibraryContext:
    """Which archived files the grid shows, in which order. folder_id None = every folder."""

    folder_id: int | None = None
    include_subfolders: bool = False
    sort: LibrarySort = "archived"
    direction: Direction | None = None  # None = the sort's default

    @property
    def dir(self) -> Direction:
        return self.direction or DEFAULT_DIRECTION[self.sort]

    def params(self) -> dict[str, str]:
        """Query parameters that rebuild this context (defaults left out)."""
        out: dict[str, str] = {}
        if self.folder_id is not None:
            out["folder"] = str(self.folder_id)
        if self.include_subfolders:
            out["sub"] = "1"
        if self.sort != "archived":
            out["sort"] = self.sort
        if self.direction and self.direction != DEFAULT_DIRECTION[self.sort]:
            out["dir"] = self.direction
        return out

    def query_string(self, **extra: object) -> str:
        params: dict[str, object] = dict(self.params())
        params.update({k: v for k, v in extra.items() if v is not None})
        return urlencode(params)


@dataclass
class LibraryPage:
    files: list[File]
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))


@dataclass(frozen=True)
class Neighbors:
    prev_id: int | None
    next_id: int | None
    position: int  # 1-based position of the file in the context
    total: int


def _sort_key(sort: LibrarySort) -> ColumnElement[object]:
    if sort == "collected":
        return File.collected_at
    if sort == "size":
        return File.size_bytes
    if sort == "name":
        return func.lower(func.coalesce(File.title, File.original_filename, ""))
    return File.archived_at


async def _filtered(session: AsyncSession, ctx: LibraryContext) -> Select[tuple[File]]:
    query = select(File).where(File.status == FileStatus.archived)
    if ctx.folder_id is not None:
        if ctx.include_subfolders:
            query = query.where(File.folder_id.in_(await subtree_ids(session, ctx.folder_id)))
        else:
            query = query.where(File.folder_id == ctx.folder_id)
    return query


def _ordered(query: Select[tuple[File]], ctx: LibraryContext, reverse: bool = False):
    key = _sort_key(ctx.sort)
    descending = (ctx.dir == "desc") != reverse
    if descending:
        return query.order_by(key.desc(), File.id.desc())
    return query.order_by(key.asc(), File.id.asc())


async def library_page(
    session: AsyncSession, ctx: LibraryContext, page: int = 1, per_page: int = PER_PAGE
) -> LibraryPage:
    query = await _filtered(session, ctx)
    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await session.scalars(
        _ordered(query, ctx).offset((max(page, 1) - 1) * per_page).limit(per_page)
    )
    return LibraryPage(list(rows.all()), total, max(page, 1), per_page)


async def neighbors(session: AsyncSession, ctx: LibraryContext, file_id: int) -> Neighbors | None:
    """The files before and after file_id in the context's order; None if it is not in it."""
    query = await _filtered(session, ctx)
    file = await session.scalar(query.where(File.id == file_id))
    if file is None:
        return None
    key = _sort_key(ctx.sort)
    value = await session.scalar(select(key).where(File.id == file_id))
    after = or_(key > value, and_(key == value, File.id > file_id))
    before = or_(key < value, and_(key == value, File.id < file_id))
    if ctx.dir == "desc":
        after, before = before, after
    next_id = await session.scalar(
        _ordered(query.where(after), ctx).with_only_columns(File.id).limit(1)
    )
    prev_id = await session.scalar(
        _ordered(query.where(before), ctx, reverse=True).with_only_columns(File.id).limit(1)
    )
    earlier = await session.scalar(select(func.count()).select_from(query.where(before).subquery()))
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    return Neighbors(prev_id, next_id, (earlier or 0) + 1, total or 0)
