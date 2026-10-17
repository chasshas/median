"""Review decisions: archive, discard, replace and undo (ARCHITECTURE.md section 7).

Every operation takes a list, runs in the caller's transaction and returns one result per
file: a file in the wrong status is rejected without affecting the others. Originals move
only after the transaction commits (run_after_commit); the caller commits.
"""

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_dedup import SimilarPair
from app.db.models_files import File, FileStatus
from app.db.models_library import FileTag, Tag, TagOrigin
from app.db.models_review import ReviewAction, ReviewActionKind
from app.library.folders import folder_exists, inbox_id
from app.library.tags import get_or_create_tags, normalize_tags
from app.storage import DataPaths, atomic_move, run_after_commit

CompareChoice = Literal["keep_left", "keep_right", "keep_both", "replace"]


class ReviewError(Exception):
    """A request that cannot be carried out as a whole (unknown folder, invalid compare)."""


@dataclass
class FileResult:
    file_id: int | None  # None for an unknown action id in undo()
    ok: bool
    status: str | None = None
    action_id: int | None = None
    error: str | None = None

    def as_json(self) -> dict[str, object]:
        return {
            "file_id": self.file_id,
            "ok": self.ok,
            "status": self.status,
            "action_id": self.action_id,
            "error": self.error,
        }


@dataclass
class ReviewResult:
    results: list[FileResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def action_ids(self) -> list[int]:
        return [r.action_id for r in self.results if r.ok and r.action_id is not None]

    @property
    def errors(self) -> list[str]:
        return [r.error for r in self.results if not r.ok and r.error]

    def extend(self, other: "ReviewResult") -> None:
        self.results.extend(other.results)

    def as_json(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "results": [r.as_json() for r in self.results],
            "action_ids": self.action_ids,
        }


# --- helpers ---------------------------------------------------------------------------------


def _unique(ids: Iterable[int]) -> list[int]:
    return list(dict.fromkeys(ids))


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _name(file: File) -> str:
    return (
        f"file {file.id} ({file.original_filename})"
        if file.original_filename
        else f"file {file.id}"
    )


async def _lock_files(session: AsyncSession, ids: Sequence[int]) -> dict[int, File]:
    """Lock the files rows (in id order, so concurrent requests cannot deadlock)."""
    if not ids:
        return {}
    rows = await session.scalars(
        select(File)
        .where(File.id.in_(ids))
        .order_by(File.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return {f.id: f for f in rows.all()}


def _status_error(file: File | None, file_id: int, expected: FileStatus) -> str | None:
    if file is None:
        return f"file {file_id} does not exist"
    if file.status is not expected:
        status = file.status.value.replace("_", " ")
        return f"{_name(file)} is {status}, not {expected.value.replace('_', ' ')}"
    return None


async def _target_folder(session: AsyncSession, folder_id: int | None) -> int:
    if folder_id is None:
        return await inbox_id(session)
    if not await folder_exists(session, folder_id):
        raise ReviewError(f"folder {folder_id} does not exist")
    return folder_id


def current_original(paths: DataPaths, file: File) -> Path | None:
    return paths.original_path(file.status.value, file.staging_path, file.sha_hex, file.ext)


def _missing_original(paths: DataPaths, file: File) -> str | None:
    path = current_original(paths, file)
    if path is None or not path.is_file():
        return f"the original of {_name(file)} is missing ({file.status.value.replace('_', ' ')})"
    return None


async def _activate_tags(
    session: AsyncSession, file_id: int, tags: Sequence[tuple[int, TagOrigin]]
) -> list[dict[str, Any]]:
    """Make tags active on a file. Inactive rows are re-activated with the given origin;
    active rows are left alone. Returns the previous state of every changed row (for undo)."""
    wanted: dict[int, TagOrigin] = {}
    for tag_id, origin in tags:
        wanted.setdefault(tag_id, origin)
    if not wanted:
        return []
    rows = await session.scalars(
        select(FileTag).where(FileTag.file_id == file_id, FileTag.tag_id.in_(wanted))
    )
    existing = {row.tag_id: row for row in rows.all()}
    changes: list[dict[str, Any]] = []
    for tag_id, origin in wanted.items():
        row = existing.get(tag_id)
        if row is None:
            session.add(FileTag(file_id=file_id, tag_id=tag_id, origin=origin, removed=False))
            changes.append({"tag_id": tag_id, "previous": None})
        elif row.removed:
            changes.append(
                {"tag_id": tag_id, "previous": {"origin": row.origin.value, "removed": True}}
            )
            row.removed = False
            row.origin = origin
    return changes


async def _deactivate_tags(
    session: AsyncSession, file_id: int, tag_ids: Sequence[int]
) -> list[dict[str, Any]]:
    """Mark active tags removed (any origin). Returns the previous state of each changed row."""
    if not tag_ids:
        return []
    rows = await session.scalars(
        select(FileTag).where(
            FileTag.file_id == file_id, FileTag.tag_id.in_(tag_ids), FileTag.removed.is_(False)
        )
    )
    changes: list[dict[str, Any]] = []
    for row in rows.all():
        changes.append(
            {"tag_id": row.tag_id, "previous": {"origin": row.origin.value, "removed": False}}
        )
        row.removed = True
    return changes


async def _removal_tag_ids(
    session: AsyncSession, per_file: Mapping[int, Iterable[str]]
) -> dict[int, list[int]]:
    """Tag ids by file id for the given names; unknown names are ignored (nothing to remove)."""
    wanted = {file_id: normalize_tags(names) for file_id, names in per_file.items()}
    names = {n for found in wanted.values() for n in found}
    if not names:
        return {}
    rows = await session.execute(select(Tag.name, Tag.id).where(Tag.name.in_(names)))
    ids = dict(rows.all())
    return {fid: [ids[n] for n in found if n in ids] for fid, found in wanted.items()}


async def _revert_tags(session: AsyncSession, file_id: int, changes: list[dict[str, Any]]) -> None:
    # Newest change first: one tag can be removed and then re-added by the same action.
    for change in reversed(changes):
        row = await session.get(FileTag, (file_id, change["tag_id"]))
        if row is None:
            continue
        previous = change["previous"]
        if previous is None:
            await session.delete(row)
        else:
            row.origin = TagOrigin(previous["origin"])
            row.removed = previous["removed"]


async def active_tags(session: AsyncSession, file_id: int) -> list[tuple[int, TagOrigin]]:
    rows = await session.execute(
        select(FileTag.tag_id, FileTag.origin)
        .where(FileTag.file_id == file_id, FileTag.removed.is_(False))
        .order_by(FileTag.tag_id)
    )
    return [(tag_id, origin) for tag_id, origin in rows.all()]


def _record(
    session: AsyncSession,
    file: File,
    kind: ReviewActionKind,
    group_id: uuid.UUID | None,
    tag_changes: list[dict[str, Any]] | None = None,
) -> ReviewAction:
    action = ReviewAction(
        file_id=file.id,
        action=kind,
        previous_status=file.status,
        previous_folder_id=file.folder_id,
        group_id=group_id,
        detail={
            "staging_path": file.staging_path,
            "reviewed_at": _iso(file.reviewed_at),
            "archived_at": _iso(file.archived_at),
            "discarded_at": _iso(file.discarded_at),
            "tags": tag_changes or [],
        },
    )
    session.add(action)
    return action


async def _archive_one(
    session: AsyncSession,
    paths: DataPaths,
    file: File,
    folder_id: int,
    tags: Sequence[tuple[int, TagOrigin]],
    kind: ReviewActionKind,
    now: datetime,
    group_id: uuid.UUID | None = None,
    remove: Sequence[int] = (),
) -> ReviewAction:
    """pending_review -> archived: staging -> archive/<sha>.<ext> after commit."""
    src = current_original(paths, file)
    assert src is not None
    changes = await _deactivate_tags(session, file.id, remove)
    changes += await _activate_tags(session, file.id, tags)
    action = _record(session, file, kind, group_id, changes)
    file.status = FileStatus.archived
    file.folder_id = folder_id
    file.staging_path = None
    file.reviewed_at = now
    file.archived_at = now
    run_after_commit(session, partial(atomic_move, src, paths.archive_path(file.sha_hex, file.ext)))
    await session.flush()
    return action


async def _discard_one(
    session: AsyncSession,
    paths: DataPaths,
    file: File,
    kind: ReviewActionKind,
    now: datetime,
    group_id: uuid.UUID | None = None,
) -> ReviewAction:
    """pending_review or archived -> discarded: the original moves to trash/ after commit."""
    src = current_original(paths, file)
    assert src is not None
    action = _record(session, file, kind, group_id)
    file.status = FileStatus.discarded
    file.folder_id = None
    file.staging_path = None
    file.archived_at = None
    file.reviewed_at = now
    file.discarded_at = now
    run_after_commit(session, partial(atomic_move, src, paths.trash_path(file.sha_hex, file.ext)))
    await session.flush()
    return action


# --- decisions -------------------------------------------------------------------------------


async def archive(
    session: AsyncSession,
    paths: DataPaths,
    file_ids: Iterable[int],
    folder_id: int | None = None,
    add_tags: Iterable[str] = (),
    remove_tags_per_file: Mapping[int, Iterable[str]] | None = None,
) -> ReviewResult:
    """Archive pending files into folder_id (default Inbox).

    add_tags are added to every file with origin user. remove_tags_per_file maps a file id to
    tag names that file loses (removed = true, any origin) before the tags are added. Both
    are recorded in the action, so undo restores the previous tag rows.
    """
    ids = _unique(file_ids)
    folder = await _target_folder(session, folder_id)
    tag_rows = [(t.id, TagOrigin.user) for t in await get_or_create_tags(session, add_tags)]
    removals = await _removal_tag_ids(session, remove_tags_per_file or {})
    files = await _lock_files(session, ids)
    now = _now()
    result = ReviewResult()
    for file_id in ids:
        file = files.get(file_id)
        error = _status_error(file, file_id, FileStatus.pending_review)
        if error is None:
            assert file is not None
            error = _missing_original(paths, file)
        if error is not None:
            result.results.append(FileResult(file_id, False, error=error))
            continue
        assert file is not None
        action = await _archive_one(
            session,
            paths,
            file,
            folder,
            tag_rows,
            ReviewActionKind.archive,
            now,
            remove=removals.get(file_id, ()),
        )
        result.results.append(FileResult(file_id, True, file.status.value, action.id))
    return result


async def _discard(
    session: AsyncSession, paths: DataPaths, file_ids: Iterable[int], expected: FileStatus
) -> ReviewResult:
    ids = _unique(file_ids)
    files = await _lock_files(session, ids)
    now = _now()
    result = ReviewResult()
    for file_id in ids:
        file = files.get(file_id)
        error = _status_error(file, file_id, expected)
        if error is None:
            assert file is not None
            error = _missing_original(paths, file)
        if error is not None:
            result.results.append(FileResult(file_id, False, error=error))
            continue
        assert file is not None
        action = await _discard_one(session, paths, file, ReviewActionKind.discard, now)
        result.results.append(FileResult(file_id, True, file.status.value, action.id))
    return result


async def discard(session: AsyncSession, paths: DataPaths, file_ids: Iterable[int]) -> ReviewResult:
    """Discard pending files: originals go to trash/, hashes, thumbnail, sources and tags stay."""
    return await _discard(session, paths, file_ids, FileStatus.pending_review)


async def discard_archived(
    session: AsyncSession, paths: DataPaths, file_ids: Iterable[int]
) -> ReviewResult:
    """Discard archived files (the library's discard); same rules as discard."""
    return await _discard(session, paths, file_ids, FileStatus.archived)


async def replace(
    session: AsyncSession, paths: DataPaths, new_id: int, old_id: int
) -> ReviewResult:
    """A better copy arrived: archive new_id into old_id's folder with the union of both files'
    active tags (origin kept per tag), then discard old_id. Both or neither."""
    if new_id == old_id:
        return ReviewResult([FileResult(new_id, False, error="a file cannot replace itself")])
    files = await _lock_files(session, [new_id, old_id])
    new, old = files.get(new_id), files.get(old_id)
    error = _status_error(new, new_id, FileStatus.pending_review) or _status_error(
        old, old_id, FileStatus.archived
    )
    if error is None:
        assert new is not None and old is not None
        error = _missing_original(paths, new) or _missing_original(paths, old)
    if error is not None:
        return ReviewResult(
            [FileResult(new_id, False, error=error), FileResult(old_id, False, error=error)]
        )
    assert new is not None and old is not None
    now = _now()
    group = uuid.uuid4()
    folder = old.folder_id if old.folder_id is not None else await inbox_id(session)
    tags = await active_tags(session, old.id)
    archived = await _archive_one(
        session, paths, new, folder, tags, ReviewActionKind.replace, now, group
    )
    discarded = await _discard_one(session, paths, old, ReviewActionKind.replace, now, group)
    return ReviewResult(
        [
            FileResult(new_id, True, new.status.value, archived.id),
            FileResult(old_id, True, old.status.value, discarded.id),
        ]
    )


async def compare_decision(
    session: AsyncSession,
    paths: DataPaths,
    file_id: int,
    other_id: int,
    choice: CompareChoice,
    folder_id: int | None = None,
    tags: Iterable[str] = (),
) -> ReviewResult:
    """Decide a pending file (left) against one similar file (right), all or nothing.

    keep_left: archive left; discard right (pending or archived; a discarded right stays).
    keep_right: discard left; archive right if it is pending.
    keep_both: archive left and a pending right; the pair is dismissed.
    replace: right is archived and left replaces it (see replace()).
    Raises ReviewError when any part fails; the caller rolls back.
    """
    tags = list(tags)
    if file_id == other_id:
        raise ReviewError("a file cannot be compared with itself")
    files = await _lock_files(session, [file_id, other_id])
    left, right = files.get(file_id), files.get(other_id)
    error = _status_error(left, file_id, FileStatus.pending_review)
    if error is None and right is None:
        error = f"file {other_id} does not exist"
    if error is not None:
        raise ReviewError(error)
    assert left is not None and right is not None

    result = ReviewResult()
    if choice == "replace":
        result = await replace(session, paths, file_id, other_id)
    elif choice == "keep_left":
        result.extend(await archive(session, paths, [file_id], folder_id, tags))
        if right.status is FileStatus.pending_review:
            result.extend(await discard(session, paths, [other_id]))
        elif right.status is FileStatus.archived:
            result.extend(await discard_archived(session, paths, [other_id]))
        elif right.status is not FileStatus.discarded:
            raise ReviewError(f"{_name(right)} is {right.status.value}; it cannot be discarded")
    else:
        if right.status not in (FileStatus.pending_review, FileStatus.archived):
            status = right.status.value.replace("_", " ")
            raise ReviewError(f"{_name(right)} is {status}; it cannot be kept from here")
        if choice == "keep_right":
            result.extend(await discard(session, paths, [file_id]))
        else:
            result.extend(await archive(session, paths, [file_id], folder_id, tags))
        if right.status is FileStatus.pending_review:
            result.extend(await archive(session, paths, [other_id], folder_id, tags))
        if choice == "keep_both":
            a, b = sorted((file_id, other_id))
            pairs = await session.scalars(
                select(SimilarPair).where(SimilarPair.file_a_id == a, SimilarPair.file_b_id == b)
            )
            for pair in pairs.all():
                pair.dismissed = True
    if not result.ok:
        raise ReviewError("; ".join(dict.fromkeys(result.errors)))
    return result


# --- undo ------------------------------------------------------------------------------------


def _result_status(action: ReviewAction) -> FileStatus:
    """The status an action left its file in."""
    if action.action is ReviewActionKind.archive:
        return FileStatus.archived
    if action.action is ReviewActionKind.discard:
        return FileStatus.discarded
    # replace: the new file was pending and got archived; the old one was archived.
    if action.previous_status is FileStatus.pending_review:
        return FileStatus.archived
    return FileStatus.discarded


async def _latest_action_id(session: AsyncSession, file_id: int) -> int | None:
    return await session.scalar(
        select(func.max(ReviewAction.id)).where(
            ReviewAction.file_id == file_id, ReviewAction.undone.is_(False)
        )
    )


async def _undo_error(
    session: AsyncSession, paths: DataPaths, action: ReviewAction, file: File | None
) -> str | None:
    if action.undone:
        return f"action {action.id} was already undone"
    if file is None:
        return f"file {action.file_id} does not exist"
    if await _latest_action_id(session, file.id) != action.id:
        return f"a later decision on {_name(file)} must be undone first"
    expected = _result_status(action)
    if file.status is not expected:
        status = file.status.value.replace("_", " ")
        return f"{_name(file)} is {status} now; action {action.id} cannot be undone"
    path = current_original(paths, file)
    if path is None or not path.is_file():
        if file.status is FileStatus.discarded:
            return (
                f"the original of {_name(file)} is no longer in the trash (purged after "
                "the retention period); the discard cannot be undone"
            )
        return f"the original of {_name(file)} is missing; action {action.id} cannot be undone"
    return None


async def _undo_one(
    session: AsyncSession, paths: DataPaths, action: ReviewAction, file: File
) -> None:
    src = current_original(paths, file)
    assert src is not None
    detail = action.detail or {}
    previous = action.previous_status
    if previous is FileStatus.archived:
        dest = paths.archive_path(file.sha_hex, file.ext)
        folder = action.previous_folder_id
        if folder is None or not await folder_exists(session, folder):
            folder = await inbox_id(session)
        file.folder_id = folder
        file.staging_path = None
    else:
        staging_path = detail.get("staging_path")
        if not staging_path:
            raise ReviewError(f"action {action.id} has no staging path to restore")
        dest = paths.resolve(staging_path)
        if dest.exists():  # the name was reused meanwhile; never overwrite another file
            dest = dest.with_name(f"{dest.stem}-{uuid.uuid4().hex[:8]}{dest.suffix}")
            staging_path = paths.relative(dest)
        file.folder_id = None
        file.staging_path = staging_path
    file.status = previous
    file.reviewed_at = _parse(detail.get("reviewed_at"))
    file.archived_at = _parse(detail.get("archived_at"))
    file.discarded_at = _parse(detail.get("discarded_at"))
    await _revert_tags(session, file.id, detail.get("tags") or [])
    action.undone = True
    if src != dest:
        run_after_commit(session, partial(atomic_move, src, dest))
    await session.flush()


async def undo(session: AsyncSession, paths: DataPaths, action_ids: Iterable[int]) -> ReviewResult:
    """Reverse decisions, newest first. Only the most recent non-undone action of a file can be
    undone; the two rows of a replace are undone together or not at all."""
    requested = _unique(action_ids)
    rows = await session.scalars(
        select(ReviewAction).where(ReviewAction.id.in_(requested)).with_for_update()
    )
    actions = {a.id: a for a in rows.all()}
    groups = {a.group_id for a in actions.values() if a.group_id is not None}
    if groups:
        members = await session.scalars(
            select(ReviewAction).where(ReviewAction.group_id.in_(groups)).with_for_update()
        )
        for member in members.all():
            actions.setdefault(member.id, member)

    result = ReviewResult()
    for action_id in requested:
        if action_id not in actions:
            result.results.append(
                FileResult(
                    None, False, action_id=action_id, error=f"action {action_id} does not exist"
                )
            )
    files = await _lock_files(session, sorted({a.file_id for a in actions.values()}))

    # Units of work: a replace group, or a single action. Newest first.
    units: dict[object, list[ReviewAction]] = {}
    for action in sorted(actions.values(), key=lambda a: a.id, reverse=True):
        units.setdefault(action.group_id or action.id, []).append(action)
    for unit in units.values():
        errors = [await _undo_error(session, paths, a, files.get(a.file_id)) for a in unit]
        if any(errors):
            message = "; ".join(e for e in dict.fromkeys(errors) if e)
            result.results.extend(
                FileResult(a.file_id, False, action_id=a.id, error=message) for a in unit
            )
            continue
        for action in unit:
            file = files[action.file_id]
            await _undo_one(session, paths, action, file)
            result.results.append(FileResult(file.id, True, file.status.value, action.id))
    return result
