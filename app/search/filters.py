"""Search filters and the query builder (ARCHITECTURE.md section 12).

SearchFilter holds every filter and the sort; search() turns it into one SELECT over files with
keyset pagination on (sort key, id) and returns the page with the total count. Tags are given by
name and resolved to ids first, so every tag condition is an EXISTS on the partial index
file_tags (tag_id, file_id) WHERE removed = false; an unknown tag in tags_all matches nothing.
"""

import base64
import binascii
import json
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import (
    ColumnElement,
    String,
    and_,
    cast,
    exists,
    false,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models_files import File, FileSource, FileStatus, MediaKind
from app.db.models_library import FileTag, Tag
from app.library.folders import subtree_ids

SearchSort = Literal[
    "archived_desc",
    "archived_asc",
    "collected_desc",
    "collected_asc",
    "size_desc",
    "size_asc",
    "random",
]
SORTS: tuple[SearchSort, ...] = (
    "archived_desc",
    "archived_asc",
    "collected_desc",
    "collected_asc",
    "size_desc",
    "size_asc",
    "random",
)
SORT_LABELS: dict[SearchSort, str] = {
    "archived_desc": "archived, newest first",
    "archived_asc": "archived, oldest first",
    "collected_desc": "collected, newest first",
    "collected_asc": "collected, oldest first",
    "size_desc": "size, largest first",
    "size_asc": "size, smallest first",
    "random": "random",
}
# Statuses the search can select (the query syntax's status: values map onto them).
SearchStatus = Literal["pending_review", "archived", "discarded"]
SEARCH_STATUSES: tuple[SearchStatus, ...] = ("pending_review", "archived", "discarded")
DEFAULT_LIMIT = 60
MAX_LIMIT = 500
MAX_SEED = 2**31 - 1


class CursorError(ValueError):
    pass


class SearchFilter(BaseModel):
    """Every filter and sort of section 12. Empty lists and None mean "no condition"."""

    folder_id: int | None = None
    folder_path: str | None = None  # "A/B", used by format(); set by parse() or the API
    include_subfolders: bool = True
    tags_all: list[str] = Field(default_factory=list)
    tags_any: list[str] = Field(default_factory=list)
    tags_none: list[str] = Field(default_factory=list)
    collected_from: date | None = None
    collected_to: date | None = None
    archived_from: date | None = None
    archived_to: date | None = None
    media_kinds: list[MediaKind] = Field(default_factory=list)
    source_keys: list[str] = Field(default_factory=list)
    statuses: list[SearchStatus] = Field(default_factory=lambda: ["archived"])
    text: list[str] = Field(default_factory=list)  # every phrase must match
    sort: SearchSort = "archived_desc"
    seed: int | None = Field(default=None, ge=0, le=MAX_SEED)

    @model_validator(mode="after")
    def _clean(self) -> "SearchFilter":
        self.tags_all = _unique(self.tags_all)
        self.tags_any = _unique(self.tags_any)
        self.tags_none = _unique(self.tags_none)
        self.media_kinds = _unique(self.media_kinds)
        self.source_keys = _unique(s for s in self.source_keys if s)
        self.statuses = _unique(self.statuses) or ["archived"]
        self.text = _unique(t for t in self.text if t.strip())
        if self.folder_id is None:
            self.folder_path = None
        return self

    def conditions(self) -> dict[str, object]:
        """The filters only (no sort or seed), for comparing two filters."""
        return self.model_dump(exclude={"sort", "seed", "folder_path"})

    def with_seed(self) -> "SearchFilter":
        """A random sort needs a seed; pick one when it has none."""
        if self.sort == "random" and self.seed is None:
            return self.model_copy(update={"seed": secrets.randbelow(MAX_SEED)})
        return self


def _unique[T](items: Iterable[T]) -> list[T]:
    return list(dict.fromkeys(items))


# --- dates -----------------------------------------------------------------------------------


def day_bounds(
    start: date | None, end: date | None, tz: str | None = None
) -> tuple[datetime | None, datetime | None]:
    """Whole local days start..end (inclusive) in APP_TIMEZONE as UTC [lower, upper)."""
    zone = ZoneInfo(tz or get_settings().app_timezone)

    def midnight(day: date) -> datetime:
        return datetime.combine(day, time(0), zone).astimezone(UTC)

    lower = midnight(start) if start else None
    upper = midnight(end + timedelta(days=1)) if end else None
    return lower, upper


def _date_range(
    column: ColumnElement[datetime | None], start: date | None, end: date | None
) -> list[ColumnElement[bool]]:
    lower, upper = day_bounds(start, end)
    out: list[ColumnElement[bool]] = []
    if lower is not None:
        out.append(column >= lower)
    if upper is not None:
        out.append(column < upper)
    return out


# --- conditions ------------------------------------------------------------------------------


async def tag_ids(session: AsyncSession, names: Iterable[str]) -> dict[str, int]:
    names = list(names)
    if not names:
        return {}
    rows = await session.execute(select(Tag.name, Tag.id).where(Tag.name.in_(names)))
    return {name: tag_id for name, tag_id in rows.all()}


def _has_tag(ids: list[int]) -> ColumnElement[bool]:
    tag = FileTag.tag_id == ids[0] if len(ids) == 1 else FileTag.tag_id.in_(ids)
    return exists().where(FileTag.file_id == File.id, tag, FileTag.removed.is_(False))


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _text_match(phrase: str) -> ColumnElement[bool]:
    pattern = f"%{_like_escape(phrase.strip())}%"
    return or_(
        *(
            column.ilike(pattern, escape="\\")
            for column in (File.title, File.original_filename, File.description)
        )
    )


async def conditions(session: AsyncSession, flt: SearchFilter) -> list[ColumnElement[bool]]:
    """WHERE conditions for every filter of flt."""
    out: list[ColumnElement[bool]] = [
        File.status.in_([FileStatus(s) for s in flt.statuses])
        if len(flt.statuses) > 1
        else File.status == FileStatus(flt.statuses[0])
    ]
    ids = await tag_ids(session, [*flt.tags_all, *flt.tags_any, *flt.tags_none])
    for name in flt.tags_all:
        out.append(_has_tag([ids[name]]) if name in ids else false())
    if flt.tags_any:
        known = [ids[n] for n in flt.tags_any if n in ids]
        out.append(_has_tag(known) if known else false())
    for name in flt.tags_none:
        if name in ids:
            out.append(~_has_tag([ids[name]]))
    if flt.folder_id is not None:
        if flt.include_subfolders:
            folder_ids = await subtree_ids(session, flt.folder_id)
            out.append(File.folder_id.in_(folder_ids) if folder_ids else false())
        else:
            out.append(File.folder_id == flt.folder_id)
    out += _date_range(File.collected_at, flt.collected_from, flt.collected_to)
    out += _date_range(File.archived_at, flt.archived_from, flt.archived_to)
    if flt.media_kinds:
        out.append(File.media_kind.in_(flt.media_kinds))
    if flt.source_keys:
        source = FileSource.source_key.in_(flt.source_keys)
        out.append(exists().where(FileSource.file_id == File.id, source))
    out += [_text_match(t) for t in flt.text]
    return out


# --- sort and cursor -------------------------------------------------------------------------


@dataclass(frozen=True)
class SortSpec:
    key: ColumnElement[object]
    descending: bool
    nullable: bool  # archived_at is NULL for files that are not archived; NULLs come last
    kind: Literal["time", "int", "text"]


def sort_spec(flt: SearchFilter) -> SortSpec:
    if flt.sort == "random":
        seed = flt.seed or 0
        key = func.md5(literal(f"{seed}:") + cast(File.id, String))
        return SortSpec(key, False, False, "text")
    name, direction = flt.sort.rsplit("_", 1)
    descending = direction == "desc"
    if name == "archived":
        return SortSpec(File.archived_at, descending, True, "time")
    if name == "collected":
        return SortSpec(File.collected_at, descending, False, "time")
    return SortSpec(File.size_bytes, descending, False, "int")


def _order_by(spec: SortSpec) -> list[ColumnElement[object]]:
    if spec.descending:
        key = spec.key.desc().nulls_last() if spec.nullable else spec.key.desc()
        return [key, File.id.desc()]
    key = spec.key.asc().nulls_last() if spec.nullable else spec.key.asc()
    return [key, File.id.asc()]


def _after(spec: SortSpec, value: object, last_id: int) -> ColumnElement[bool]:
    """Rows that come after (value, last_id) in the order of spec (NULL keys last)."""
    id_after = File.id < last_id if spec.descending else File.id > last_id
    if value is None:
        return and_(spec.key.is_(None), id_after)
    key_after = spec.key < value if spec.descending else spec.key > value
    out = or_(key_after, and_(spec.key == value, id_after))
    return or_(out, spec.key.is_(None)) if spec.nullable else out


def encode_cursor(flt: SearchFilter, value: object, last_id: int) -> str:
    if isinstance(value, datetime):
        value = value.isoformat()
    payload = json.dumps([flt.sort, flt.seed, value, last_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(flt: SearchFilter, cursor: str) -> tuple[object, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        sort, seed, value, last_id = json.loads(raw)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise CursorError("invalid cursor") from exc
    if sort != flt.sort or seed != flt.seed or not isinstance(last_id, int):
        raise CursorError("the cursor belongs to another sort; start from the first page")
    kind = sort_spec(flt).kind
    try:
        if value is None:
            pass
        elif kind == "time":
            value = datetime.fromisoformat(value)
        elif kind == "int":
            value = int(value)
        elif not isinstance(value, str):
            raise TypeError(value)
    except (TypeError, ValueError) as exc:
        raise CursorError("invalid cursor") from exc
    return value, last_id


# --- search ----------------------------------------------------------------------------------


@dataclass
class SearchPage:
    files: list[File]
    total: int
    next_cursor: str | None
    filter: SearchFilter


async def search(
    session: AsyncSession,
    flt: SearchFilter,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> SearchPage:
    """One page of files matching flt, after cursor (None = first page)."""
    flt = flt.with_seed()
    where = await conditions(session, flt)
    spec = sort_spec(flt)
    query = select(File, spec.key.label("sort_key")).where(*where)
    if cursor:
        value, last_id = decode_cursor(flt, cursor)
        query = query.where(_after(spec, value, last_id))
    rows = (await session.execute(query.order_by(*_order_by(spec)).limit(limit + 1))).all()
    total = await session.scalar(select(func.count()).select_from(File).where(*where)) or 0
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last_file, last_value = page[-1]
        next_cursor = encode_cursor(flt, last_value, last_file.id)
    return SearchPage([f for f, _ in page], total, next_cursor, flt)
