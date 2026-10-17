"""Tag normalization and tag rows (ARCHITECTURE.md section 9)."""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import Select, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_library import FileTag, Tag, TagOrigin

MAX_TAG_LENGTH = 100
_WHITESPACE = re.compile(r"\s+")
_NAMESPACE = re.compile(r"[a-z0-9_]+")


def _normalize_part(text: str) -> str:
    text = _WHITESPACE.sub("_", text.strip().lower())
    return text.strip("_")


def normalize_tag(raw: str) -> str | None:
    """Normalize a tag; None when nothing is left.

    NFKC, trim, lowercase, whitespace runs -> "_", trim "_", at most 100 characters.
    "ns:value" keeps its namespace when ns matches [a-z0-9_]+ (after normalizing each side).
    """
    text = unicodedata.normalize("NFKC", raw)
    ns, sep, value = text.partition(":")
    if sep:
        ns_norm, value_norm = _normalize_part(ns), _normalize_part(value)
        if ns_norm and _NAMESPACE.fullmatch(ns_norm):
            if not value_norm:
                return None
            return _truncate(f"{ns_norm}:{value_norm}")
    return _truncate(_normalize_part(text))


def _truncate(tag: str) -> str | None:
    tag = tag[:MAX_TAG_LENGTH].rstrip("_")
    if tag.endswith(":"):
        return None
    return tag or None


def normalize_tags(raw: Iterable[str]) -> list[str]:
    """Normalize, drop empties and duplicates, keep first-seen order."""
    seen: dict[str, None] = {}
    for item in raw:
        tag = normalize_tag(item)
        if tag is not None:
            seen.setdefault(tag, None)
    return list(seen)


def split_tag_input(text: str | None) -> list[str]:
    """Split a comma-separated tag field as typed by the user."""
    return [part for part in (text or "").split(",") if part.strip()]


async def get_or_create_tags(session: AsyncSession, raw: Iterable[str]) -> list[Tag]:
    """Tag rows for the given names (normalized), creating missing ones. Order follows input."""
    names = normalize_tags(raw)
    if not names:
        return []
    await session.execute(
        insert(Tag)
        .values([{"name": n} for n in names])
        .on_conflict_do_nothing(index_elements=[Tag.name])
    )
    rows = (await session.scalars(select(Tag).where(Tag.name.in_(names)))).all()
    by_name = {t.name: t for t in rows}
    return [by_name[n] for n in names]


async def merge_crawler_tags(session: AsyncSession, file_id: int, raw: Iterable[str]) -> None:
    """Add crawler tags unless the file already has a row for that tag (removed or not)."""
    tags = await get_or_create_tags(session, raw)
    if not tags:
        return
    await session.execute(
        insert(FileTag)
        .values([{"file_id": file_id, "tag_id": t.id, "origin": TagOrigin.crawler} for t in tags])
        .on_conflict_do_nothing(index_elements=[FileTag.file_id, FileTag.tag_id])
    )


async def add_user_tags(session: AsyncSession, file_id: int, raw: Iterable[str]) -> None:
    """Add tags as the user: re-adding a removed tag sets removed = false and origin = user."""
    tags = await get_or_create_tags(session, raw)
    if not tags:
        return
    stmt = insert(FileTag).values(
        [{"file_id": file_id, "tag_id": t.id, "origin": TagOrigin.user} for t in tags]
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[FileTag.file_id, FileTag.tag_id],
            set_={"removed": False, "origin": TagOrigin.user},
            where=FileTag.removed.is_(True),
        )
    )


# --- tag management (ARCHITECTURE.md section 9) ----------------------------------------------


class TagError(ValueError):
    """A tag operation that is not allowed (empty name, in use without force)."""


class TagNotFound(LookupError):
    pass


class TagInUse(TagError):
    pass


@dataclass(frozen=True)
class TagUsage:
    id: int
    name: str
    count: int  # files with an active (not removed) row

    def as_json(self) -> dict[str, object]:
        return {"id": self.id, "name": self.name, "count": self.count}


@dataclass(frozen=True)
class RenameResult:
    tag: TagUsage
    merged_from: str | None  # the old name when it was merged into an existing tag


def valid_tag(raw: str) -> str:
    name = normalize_tag(raw)
    if name is None:
        raise TagError(f"{raw!r} is empty after normalization")
    return name


def _usage_query() -> Select[tuple[int, str, int]]:
    active = func.count(FileTag.file_id).filter(FileTag.removed.is_(False))
    return (
        select(Tag.id, Tag.name, active.label("count"))
        .outerjoin(FileTag, FileTag.tag_id == Tag.id)
        .group_by(Tag.id)
    )


async def get_tag(session: AsyncSession, tag_id: int) -> TagUsage:
    row = (await session.execute(_usage_query().where(Tag.id == tag_id))).one_or_none()
    if row is None:
        raise TagNotFound(f"tag {tag_id} does not exist")
    return TagUsage(*row)


async def find_tag(session: AsyncSession, name: str) -> TagUsage | None:
    normalized = normalize_tag(name)
    if normalized is None:
        return None
    row = (await session.execute(_usage_query().where(Tag.name == normalized))).one_or_none()
    return TagUsage(*row) if row else None


async def list_tags(session: AsyncSession, contains: str | None = None) -> list[TagUsage]:
    """Every tag with its usage count (active rows only), by name. contains filters by
    substring (case-insensitive, on the normalized form when it has one)."""
    query = _usage_query().order_by(Tag.name)
    if contains and contains.strip():
        needle = normalize_tag(contains) or contains.strip().lower()
        query = query.where(Tag.name.contains(needle, autoescape=True))
    return [TagUsage(*row) for row in (await session.execute(query)).all()]


async def autocomplete(session: AsyncSession, prefix: str, limit: int = 10) -> list[TagUsage]:
    """Tags starting with the (normalized) prefix, most used first, then by name."""
    needle = _prefix_needle(prefix)
    if needle is None:
        return []
    query = (
        _usage_query()
        .where(Tag.name.startswith(needle, autoescape=True))
        .order_by(text("count DESC"), Tag.name)
        .limit(limit)
    )
    return [TagUsage(*row) for row in (await session.execute(query)).all()]


def _prefix_needle(raw: str) -> str | None:
    """Normalize a partly typed tag, keeping a trailing separator so "blue " and "artist:"
    narrow the matches as the full tag would ("blue_...", "artist:...")."""
    text = unicodedata.normalize("NFKC", raw).lower()
    if text.rstrip().endswith(":"):
        ns = normalize_tag(text.rstrip()[:-1])
        return f"{ns}:" if ns and _NAMESPACE.fullmatch(ns) else ns
    needle = normalize_tag(text)
    if needle and text.strip() and (text[-1].isspace() or text.endswith("_")):
        needle += "_"
    return needle


async def rename_tag(session: AsyncSession, tag_id: int, new_name: str) -> RenameResult:
    """Rename a tag. If a tag with the new name exists, merge into it: file_tags rows are
    re-pointed; when a file has both, one row stays, active if either was active, origin user
    if a deciding row was user (the active one wins over a removed one)."""
    tag = await session.get(Tag, tag_id, with_for_update=True)
    if tag is None:
        raise TagNotFound(f"tag {tag_id} does not exist")
    name = valid_tag(new_name)
    if name == tag.name:
        return RenameResult(await get_tag(session, tag_id), None)
    target = await session.scalar(select(Tag).where(Tag.name == name).with_for_update())
    if target is None:
        tag.name = name
        await session.flush()
        return RenameResult(await get_tag(session, tag_id), None)

    params = {"src": tag.id, "dst": target.id}
    await session.execute(
        text(
            "UPDATE file_tags t SET"
            "  removed = t.removed AND s.removed,"
            "  origin = CASE"
            "    WHEN t.removed = s.removed THEN"
            "      CASE WHEN 'user' IN (t.origin, s.origin)"
            "      THEN 'user'::tag_origin ELSE 'crawler'::tag_origin END"
            "    WHEN NOT t.removed THEN t.origin"
            "    ELSE s.origin END,"
            "  created_at = least(t.created_at, s.created_at)"
            " FROM file_tags s"
            " WHERE s.tag_id = :src AND t.tag_id = :dst AND t.file_id = s.file_id"
        ),
        params,
    )
    await session.execute(
        text(
            "UPDATE file_tags SET tag_id = :dst WHERE tag_id = :src AND file_id NOT IN"
            " (SELECT file_id FROM file_tags WHERE tag_id = :dst)"
        ),
        params,
    )
    old_name = tag.name
    await session.delete(tag)  # its remaining (conflicting) rows go with it (on delete cascade)
    await session.flush()
    return RenameResult(await get_tag(session, target.id), old_name)


async def delete_tag(session: AsyncSession, tag_id: int, force: bool = False) -> TagUsage:
    """Delete a tag and every file_tags row of it. A tag in use needs force."""
    usage = await get_tag(session, tag_id)
    if usage.count and not force:
        raise TagInUse(f'"{usage.name}" is used by {usage.count} file(s); confirm to delete it')
    await session.execute(delete(Tag).where(Tag.id == tag_id))
    return usage


# --- per-file tags ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileTagRow:
    tag_id: int
    name: str
    origin: str
    removed: bool

    def as_json(self) -> dict[str, object]:
        return {"id": self.tag_id, "name": self.name, "origin": self.origin}


async def file_tags(session: AsyncSession, file_id: int) -> list[FileTagRow]:
    """All tag rows of a file (active and removed), by name."""
    rows = await session.execute(
        select(Tag.id, Tag.name, FileTag.origin, FileTag.removed)
        .join(FileTag, FileTag.tag_id == Tag.id)
        .where(FileTag.file_id == file_id)
        .order_by(Tag.name)
    )
    return [FileTagRow(tid, name, origin.value, removed) for tid, name, origin, removed in rows]


async def add_user_tag(session: AsyncSession, file_id: int, name: str) -> Tag:
    """Add one tag as the user (section 9): a new row has origin user, a removed row becomes
    active with origin user, an active row is left as it is."""
    normalized = valid_tag(name)
    await add_user_tags(session, file_id, [normalized])
    tag = await session.scalar(select(Tag).where(Tag.name == normalized))
    assert tag is not None
    return tag


async def _set_removed(session: AsyncSession, file_id: int, tag_id: int, removed: bool) -> bool:
    result = await session.execute(
        update(FileTag)
        .where(FileTag.file_id == file_id, FileTag.tag_id == tag_id)
        .values(removed=removed)
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def remove_tag(session: AsyncSession, file_id: int, tag_id: int) -> bool:
    """Mark a file's tag removed (any origin); the row stays so recrawls never re-add it.
    False when the file has no row for that tag."""
    return await _set_removed(session, file_id, tag_id, True)


async def restore_tag(session: AsyncSession, file_id: int, tag_id: int) -> bool:
    """Make a removed tag active again, keeping its origin."""
    return await _set_removed(session, file_id, tag_id, False)
