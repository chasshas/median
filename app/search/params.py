"""Search request parameters: either q (the query syntax) or filter fields (the filter panel and
the JSON API), plus sort, seed, cursor and limit. Empty form values mean "no condition"."""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated

from fastapi import HTTPException, Query, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_files import MediaKind
from app.library.folders import folder_exists
from app.library.tags import normalize_tags
from app.search.filters import DEFAULT_LIMIT, MAX_LIMIT, SearchFilter, SearchSort, tag_ids
from app.search.query import (
    STATUS_WORDS,
    Parsed,
    SearchSyntaxError,
    SearchWarning,
    parse,
    with_folder_path,
)

_WORDS = re.compile(r"[\s,]+")
FIELD_NAMES = (
    "folder",
    "exact",
    "tags_all",
    "tags_any",
    "tags_none",
    "collected_from",
    "collected_to",
    "archived_from",
    "archived_to",
    "type",
    "source",
    "status",
    "text",
)


def split_words(values: list[str]) -> list[str]:
    """Tags and source keys typed into one field, separated by commas or whitespace."""
    return [w for v in values for w in _WORDS.split(v) if w]


def _flag(value: str | None) -> bool:
    return (value or "").lower() in ("1", "true", "on", "yes")


def _date(name: str, value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a date (YYYY-MM-DD)") from exc


@dataclass
class SearchParams:
    q: str | None = None
    folder: str | None = None
    exact: str | None = None
    tags_all: list[str] = field(default_factory=list)
    tags_any: list[str] = field(default_factory=list)
    tags_none: list[str] = field(default_factory=list)
    collected_from: str | None = None
    collected_to: str | None = None
    archived_from: str | None = None
    archived_to: str | None = None
    type: list[str] = field(default_factory=list)
    source: list[str] = field(default_factory=list)
    status: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    sort: SearchSort = "archived_desc"
    seed: int | None = None
    cursor: str | None = None
    limit: int = DEFAULT_LIMIT

    def filter_fields(self) -> SearchFilter:
        """The filter given by the fields (q ignored); ValueError for invalid values."""
        folder_id = None
        if self.folder:
            try:
                folder_id = int(self.folder)
            except ValueError as exc:
                raise ValueError("folder must be an id") from exc
        statuses = []
        for word in self.status:
            if word:
                statuses.append(STATUS_WORDS.get(word, word))
        try:
            return SearchFilter(
                folder_id=folder_id,
                include_subfolders=not _flag(self.exact),
                tags_all=normalize_tags(split_words(self.tags_all)),
                tags_any=normalize_tags(split_words(self.tags_any)),
                tags_none=normalize_tags(split_words(self.tags_none)),
                collected_from=_date("collected_from", self.collected_from),
                collected_to=_date("collected_to", self.collected_to),
                archived_from=_date("archived_from", self.archived_from),
                archived_to=_date("archived_to", self.archived_to),
                media_kinds=[MediaKind(t) for t in self.type if t],
                source_keys=split_words(self.source),
                statuses=statuses,
                text=self.text,
                sort=self.sort,
                seed=self.seed,
            )
        except ValidationError as exc:
            raise ValueError("; ".join(e["msg"] for e in exc.errors())) from exc

    def has_fields(self) -> bool:
        return any(getattr(self, name) for name in FIELD_NAMES)


def search_params(
    q: str | None = None,
    folder: str | None = None,
    exact: str | None = None,
    tags_all: Annotated[list[str] | None, Query()] = None,
    tags_any: Annotated[list[str] | None, Query()] = None,
    tags_none: Annotated[list[str] | None, Query()] = None,
    collected_from: str | None = None,
    collected_to: str | None = None,
    archived_from: str | None = None,
    archived_to: str | None = None,
    type: Annotated[list[str] | None, Query()] = None,
    source: Annotated[list[str] | None, Query()] = None,
    status: Annotated[list[str] | None, Query()] = None,
    text: Annotated[list[str] | None, Query()] = None,
    sort: SearchSort = "archived_desc",
    seed: Annotated[int | None, Query(ge=0, le=2**31 - 1)] = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> SearchParams:
    return SearchParams(
        q,
        folder,
        exact,
        tags_all or [],
        tags_any or [],
        tags_none or [],
        collected_from,
        collected_to,
        archived_from,
        archived_to,
        type or [],
        source or [],
        status or [],
        text or [],
        sort,
        seed,
        cursor or None,
        limit,
    )


async def resolve(session: AsyncSession, params: SearchParams) -> Parsed:
    """The filter (with sort and seed) for the request: q when given, else the fields.

    Raises HTTPException 400 with {message, position} for a syntax error.
    """
    if params.q is not None and not params.has_fields():
        try:
            parsed = await parse(session, params.q)
        except SearchSyntaxError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.as_json()) from exc
        parsed.filter = parsed.filter.model_copy(update={"sort": params.sort, "seed": params.seed})
        return parsed
    try:
        flt = params.filter_fields()
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"message": str(exc)}) from exc
    if flt.folder_id is not None and not await folder_exists(session, flt.folder_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"message": "unknown folder"})
    flt = await with_folder_path(session, flt)
    known = await tag_ids(session, [*flt.tags_all, *flt.tags_any, *flt.tags_none])
    warnings = [
        SearchWarning(f'no file has the tag "{name}"', None)
        for name in dict.fromkeys([*flt.tags_all, *flt.tags_any, *flt.tags_none])
        if name not in known
    ]
    return Parsed(flt, warnings)


def panel_fields(flt: SearchFilter) -> dict[str, object]:
    """Values for the filter panel's inputs (the inverse of SearchParams.filter_fields)."""

    def day(value: date | None) -> str:
        return value.isoformat() if value else ""

    keyword = {v: k for k, v in STATUS_WORDS.items()}
    return {
        "folder": str(flt.folder_id) if flt.folder_id is not None else "",
        "exact": not flt.include_subfolders,
        "tags_all": " ".join(flt.tags_all),
        "tags_any": " ".join(flt.tags_any),
        "tags_none": " ".join(flt.tags_none),
        "collected_from": day(flt.collected_from),
        "collected_to": day(flt.collected_to),
        "archived_from": day(flt.archived_from),
        "archived_to": day(flt.archived_to),
        "type": [k.value for k in flt.media_kinds],
        "source": " ".join(flt.source_keys),
        "status": [keyword[s] for s in flt.statuses],
        "text": list(flt.text),
    }
