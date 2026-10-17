"""Classification of archived files: folder moves, bulk tags and file details.

Moving a file between folders changes files.folder_id only; the original stays at
archive/<sha>.<ext> (section 5), so nothing on disk is touched. The caller commits.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_files import File, FileStatus
from app.db.models_library import FileTag, Tag
from app.library.folders import get_folder
from app.library.tags import add_user_tags, normalize_tags

MAX_TITLE = 500
MAX_DESCRIPTION = 20_000


class ClassifyError(ValueError):
    pass


@dataclass
class BulkResult:
    """Per-file outcome of a bulk action: ok ids and messages for the rejected ones."""

    ok_ids: list[int] = field(default_factory=list)
    errors: dict[int, str] = field(default_factory=dict)

    def as_json(self) -> dict[str, object]:
        return {
            "ok": not self.errors,
            "results": [{"file_id": i, "ok": True} for i in self.ok_ids]
            + [{"file_id": i, "ok": False, "error": e} for i, e in self.errors.items()],
        }


async def _archived(
    session: AsyncSession, file_ids: Iterable[int], lock: bool = False
) -> tuple[list[File], BulkResult]:
    ids = list(dict.fromkeys(file_ids))
    query = select(File).where(File.id.in_(ids)).order_by(File.id)
    if lock:
        query = query.with_for_update()
    found = {f.id: f for f in (await session.scalars(query)).all()}
    result = BulkResult()
    files: list[File] = []
    for file_id in ids:
        file = found.get(file_id)
        if file is None:
            result.errors[file_id] = f"file {file_id} does not exist"
        elif file.status is not FileStatus.archived:
            status = file.status.value.replace("_", " ")
            result.errors[file_id] = f"file {file_id} is {status}, not archived"
        else:
            files.append(file)
    return files, result


async def move_files(session: AsyncSession, file_ids: Iterable[int], folder_id: int) -> BulkResult:
    """Put archived files into another folder (database only)."""
    await get_folder(session, folder_id)
    files, result = await _archived(session, file_ids, lock=True)
    for file in files:
        file.folder_id = folder_id
        result.ok_ids.append(file.id)
    await session.flush()
    return result


async def add_tags_to_files(
    session: AsyncSession, file_ids: Iterable[int], names: Iterable[str]
) -> BulkResult:
    """Add tags as the user to archived files (section 9 rules, see add_user_tags)."""
    tags = normalize_tags(names)
    if not tags:
        raise ClassifyError("no tag left after normalization")
    files, result = await _archived(session, file_ids)
    for file in files:
        await add_user_tags(session, file.id, tags)
        result.ok_ids.append(file.id)
    return result


async def remove_tags_from_files(
    session: AsyncSession, file_ids: Iterable[int], names: Iterable[str]
) -> BulkResult:
    """Mark the tags removed on archived files (any origin); files without them are unchanged."""
    tags = normalize_tags(names)
    if not tags:
        raise ClassifyError("no tag left after normalization")
    files, result = await _archived(session, file_ids)
    tag_ids = list((await session.scalars(select(Tag.id).where(Tag.name.in_(tags)))).all())
    if files and tag_ids:
        await session.execute(
            update(FileTag)
            .where(FileTag.file_id.in_([f.id for f in files]), FileTag.tag_id.in_(tag_ids))
            .values(removed=True)
        )
    result.ok_ids.extend(f.id for f in files)
    return result


def _clean_text(value: str | None, limit: int, label: str) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if len(value) > limit:
        raise ClassifyError(f"the {label} can have at most {limit} characters")
    return value or None


async def update_details(session: AsyncSession, file_id: int, changes: dict[str, object]) -> File:
    """Apply the given fields: title, description (empty = none) and folder_id (archived only)."""
    file = await session.get(File, file_id, with_for_update=True)
    if file is None:
        raise LookupError(f"file {file_id} does not exist")
    if "title" in changes:
        file.title = _clean_text(changes["title"], MAX_TITLE, "title")  # type: ignore[arg-type]
    if "description" in changes:
        file.description = _clean_text(
            changes["description"],  # type: ignore[arg-type]
            MAX_DESCRIPTION,
            "description",
        )
    if "folder_id" in changes:
        folder_id = changes["folder_id"]
        if file.status is not FileStatus.archived:
            raise ClassifyError("only archived files have a folder")
        if not isinstance(folder_id, int):
            raise ClassifyError("an archived file must be in a folder")
        await get_folder(session, folder_id)
        file.folder_id = folder_id
    await session.flush()
    return file
