"""Tag normalization and tag rows (ARCHITECTURE.md section 9)."""

import re
import unicodedata
from collections.abc import Iterable

from sqlalchemy import select
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
