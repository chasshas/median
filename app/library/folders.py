"""Folders: one per archived file, nested, with sibling-unique names (ARCHITECTURE.md section 10).

Every function runs in the caller's transaction; the caller commits. Moving files between
folders only changes files.folder_id: archive paths depend on the sha, never on the folder.
"""

import unicodedata
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_files import File, FileStatus
from app.db.models_library import INBOX_NAME, Folder

MAX_FOLDER_NAME = 100
PATH_SEPARATOR = " / "


class FolderError(ValueError):
    """A folder operation that is not allowed (clash, cycle, Inbox, not empty, bad name)."""


class FolderNotFound(LookupError):
    pass


class FolderNotEmpty(FolderError):
    pass


@dataclass(frozen=True)
class FolderChoice:
    id: int
    path: str  # "A / B / C"


@dataclass
class FolderNode:
    id: int
    parent_id: int | None
    name: str
    path: str
    depth: int
    direct_count: int  # archived files directly in this folder
    subtree_count: int = 0  # archived files in this folder and every subfolder
    is_inbox: bool = False
    children: list["FolderNode"] = field(default_factory=list)

    def as_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "name": self.name,
            "path": self.path,
            "direct_count": self.direct_count,
            "subtree_count": self.subtree_count,
            "is_inbox": self.is_inbox,
            "children": [c.as_json() for c in self.children],
        }


@dataclass
class DeleteResult:
    files_moved: int = 0
    folders_moved: int = 0
    folders_merged: int = 0

    def as_json(self) -> dict[str, int]:
        return {
            "files_moved": self.files_moved,
            "folders_moved": self.folders_moved,
            "folders_merged": self.folders_merged,
        }


# --- lookups ---------------------------------------------------------------------------------


async def inbox_id(session: AsyncSession) -> int:
    found = await session.scalar(
        select(Folder.id).where(Folder.parent_id.is_(None), Folder.name == INBOX_NAME)
    )
    if found is None:
        raise RuntimeError("the Inbox folder is missing; run make migrate")
    return found


async def folder_exists(session: AsyncSession, folder_id: int) -> bool:
    return await session.get(Folder, folder_id) is not None


async def get_folder(session: AsyncSession, folder_id: int) -> Folder:
    folder = await session.get(Folder, folder_id)
    if folder is None:
        raise FolderNotFound(f"folder {folder_id} does not exist")
    return folder


def is_inbox(folder: Folder) -> bool:
    return folder.parent_id is None and folder.name == INBOX_NAME


# Constant SQL only; queries below append constant text to it (hence the S608 noqa).
_TREE_CTE = (
    "WITH RECURSIVE tree(id, parent_id, name, path, depth) AS ("
    "  SELECT id, parent_id, name, name, 0 FROM folders WHERE parent_id IS NULL"
    "  UNION ALL"
    "  SELECT f.id, f.parent_id, f.name, tree.path || ' / ' || f.name, tree.depth + 1"
    "  FROM folders f JOIN tree ON f.parent_id = tree.id"
    ")"
)


async def folder_choices(session: AsyncSession) -> list[FolderChoice]:
    """Every folder with its display path, Inbox first, then by path."""
    rows = await session.execute(
        text(f"{_TREE_CTE} SELECT id, path FROM tree ORDER BY (path = :inbox) DESC, lower(path)"),  # noqa: S608
        {"inbox": INBOX_NAME},
    )
    return [FolderChoice(folder_id, path) for folder_id, path in rows.all()]


async def subtree_ids(session: AsyncSession, folder_id: int) -> list[int]:
    """The folder and all its descendants (recursive CTE), the folder first."""
    rows = await session.scalars(
        text(
            "WITH RECURSIVE sub(id, depth) AS ("
            "  SELECT id, 0 FROM folders WHERE id = :id"
            "  UNION ALL"
            "  SELECT f.id, sub.depth + 1 FROM folders f JOIN sub ON f.parent_id = sub.id"
            ") SELECT id FROM sub ORDER BY depth, id"
        ),
        {"id": folder_id},
    )
    return list(rows.all())


async def path_of(session: AsyncSession, folder_id: int) -> str:
    """Display path "A / B / C" (recursive CTE up to the top level)."""
    rows = await session.scalars(
        text(
            "WITH RECURSIVE up(id, parent_id, name, depth) AS ("
            "  SELECT id, parent_id, name, 0 FROM folders WHERE id = :id"
            "  UNION ALL"
            "  SELECT f.id, f.parent_id, f.name, up.depth + 1"
            "  FROM folders f JOIN up ON f.id = up.parent_id"
            ") SELECT name FROM up ORDER BY depth DESC"
        ),
        {"id": folder_id},
    )
    names = list(rows.all())
    if not names:
        raise FolderNotFound(f"folder {folder_id} does not exist")
    return PATH_SEPARATOR.join(names)


async def resolve_path(session: AsyncSession, path: str) -> int | None:
    """The folder id for "A/B" (or "A / B"), matching names case-insensitively; None if absent."""
    parts = [normalize_folder_name(p) for p in path.split("/")]
    parts = [p for p in parts if p]
    if not parts:
        return None
    parent: int | None = None
    for part in parts:
        query = select(Folder.id).where(func.lower(Folder.name) == part.lower())
        query = query.where(
            Folder.parent_id.is_(None) if parent is None else Folder.parent_id == parent
        )
        parent = await session.scalar(query)
        if parent is None:
            return None
    return parent


async def get_tree(session: AsyncSession) -> list[FolderNode]:
    """All folders as a tree with direct and subtree counts of archived files.

    Top level: Inbox first, then by name; children by name (case-insensitive).
    """
    rows = await session.execute(
        text(
            f"{_TREE_CTE} SELECT tree.id, tree.parent_id, tree.name, tree.path, tree.depth,"  # noqa: S608
            "  coalesce(c.n, 0)"
            " FROM tree LEFT JOIN ("
            "  SELECT folder_id, count(*) AS n FROM files"
            "  WHERE status = 'archived' AND folder_id IS NOT NULL GROUP BY folder_id"
            " ) c ON c.folder_id = tree.id"
            " ORDER BY tree.depth, lower(tree.name), tree.id"
        )
    )
    nodes: dict[int, FolderNode] = {}
    roots: list[FolderNode] = []
    for folder_id, parent_id, name, path, depth, count in rows.all():
        node = FolderNode(folder_id, parent_id, name, path, depth, count)
        node.is_inbox = parent_id is None and name == INBOX_NAME
        nodes[folder_id] = node
        if parent_id is None:
            roots.append(node)
        else:
            nodes[parent_id].children.append(node)

    def total(node: FolderNode) -> int:
        node.subtree_count = node.direct_count + sum(total(c) for c in node.children)
        return node.subtree_count

    for root in roots:
        total(root)
    roots.sort(key=lambda n: not n.is_inbox)  # stable: keeps name order otherwise
    return roots


def flatten(nodes: list[FolderNode]) -> list[FolderNode]:
    """Depth-first, in display order."""
    out: list[FolderNode] = []
    for node in nodes:
        out.append(node)
        out.extend(flatten(node.children))
    return out


async def recent_folders(session: AsyncSession, limit: int = 5) -> list[FolderChoice]:
    """Folders that most recently received an archived file, newest first."""
    rows = await session.execute(
        select(File.folder_id, func.max(File.archived_at).label("last"))
        .where(File.status == FileStatus.archived, File.folder_id.is_not(None))
        .group_by(File.folder_id)
        .order_by(func.max(File.archived_at).desc(), File.folder_id.desc())
        .limit(limit)
    )
    return [FolderChoice(fid, await path_of(session, fid)) for fid, _ in rows.all()]


# --- changes ---------------------------------------------------------------------------------


def normalize_folder_name(raw: str) -> str:
    return " ".join(unicodedata.normalize("NFC", raw).split())


def _valid_name(raw: str) -> str:
    name = normalize_folder_name(raw)
    if not name:
        raise FolderError("a folder name cannot be empty")
    if "/" in name:
        raise FolderError('a folder name cannot contain "/"')
    if len(name) > MAX_FOLDER_NAME:
        raise FolderError(f"a folder name can have at most {MAX_FOLDER_NAME} characters")
    return name


async def _sibling(
    session: AsyncSession, parent_id: int | None, name: str, exclude: int | None = None
) -> Folder | None:
    query = select(Folder).where(func.lower(Folder.name) == name.lower())
    query = query.where(
        Folder.parent_id.is_(None) if parent_id is None else Folder.parent_id == parent_id
    )
    if exclude is not None:
        query = query.where(Folder.id != exclude)
    return await session.scalar(query)


async def _check_clash(
    session: AsyncSession, parent_id: int | None, name: str, exclude: int | None = None
) -> None:
    clash = await _sibling(session, parent_id, name, exclude)
    if clash is not None:
        where = await path_of(session, parent_id) if parent_id is not None else "the top level"
        raise FolderError(f'a folder named "{clash.name}" already exists in {where}')


async def _flush(session: AsyncSession) -> None:
    """Flush; a concurrent sibling with the same name surfaces as a FolderError."""
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        raise FolderError("a folder with that name already exists there") from exc


def _protect_inbox(folder: Folder, verb: str) -> None:
    if is_inbox(folder):
        raise FolderError(f"the Inbox cannot be {verb}")


async def create(session: AsyncSession, name: str, parent_id: int | None = None) -> Folder:
    name = _valid_name(name)
    if parent_id is not None:
        await get_folder(session, parent_id)
    await _check_clash(session, parent_id, name)
    folder = Folder(parent_id=parent_id, name=name)
    session.add(folder)
    await _flush(session)
    return folder


async def rename(session: AsyncSession, folder_id: int, name: str) -> Folder:
    folder = await get_folder(session, folder_id)
    _protect_inbox(folder, "renamed")
    name = _valid_name(name)
    await _check_clash(session, folder.parent_id, name, exclude=folder.id)
    folder.name = name
    await _flush(session)
    return folder


async def move(session: AsyncSession, folder_id: int, new_parent_id: int | None) -> Folder:
    """Move a folder under new_parent_id (None = top level). Cycles are rejected."""
    folder = await get_folder(session, folder_id)
    _protect_inbox(folder, "moved")
    if new_parent_id is not None:
        await get_folder(session, new_parent_id)
        if new_parent_id in await subtree_ids(session, folder_id):
            raise FolderError("a folder cannot be moved into itself or one of its subfolders")
    if new_parent_id == folder.parent_id:
        return folder
    await _check_clash(session, new_parent_id, folder.name, exclude=folder.id)
    folder.parent_id = new_parent_id
    await _flush(session)
    return folder


async def _has_contents(session: AsyncSession, folder_id: int) -> bool:
    child = await session.scalar(select(Folder.id).where(Folder.parent_id == folder_id).limit(1))
    if child is not None:
        return True
    file = await session.scalar(select(File.id).where(File.folder_id == folder_id).limit(1))
    return file is not None


async def _move_files(session: AsyncSession, src: int, dest: int) -> int:
    result = await session.execute(update(File).where(File.folder_id == src).values(folder_id=dest))
    return result.rowcount or 0  # type: ignore[attr-defined]


async def _merge(
    session: AsyncSession, src: Folder, dest_parent: int | None, files_dest: int, out: DeleteResult
) -> None:
    """Move src's files to files_dest and its subfolders under dest_parent. A subfolder whose
    name clashes with a folder already there is merged into it (recursively) and deleted."""
    out.files_moved += await _move_files(session, src.id, files_dest)
    children = await session.scalars(
        select(Folder).where(Folder.parent_id == src.id).order_by(Folder.id)
    )
    for child in children.all():
        clash = await _sibling(session, dest_parent, child.name, exclude=src.id)
        if clash is None:
            child.parent_id = dest_parent
            out.folders_moved += 1
            await session.flush()
        else:
            await _merge(session, child, clash.id, clash.id, out)
            await session.delete(child)
            out.folders_merged += 1
            await session.flush()


async def delete(
    session: AsyncSession, folder_id: int, move_contents_to_parent: bool = False
) -> DeleteResult:
    """Delete a folder. A non-empty one needs move_contents_to_parent: its files go to the parent
    (the Inbox for a top-level folder) and its subfolders move up, merging on name clashes."""
    folder = await get_folder(session, folder_id)
    _protect_inbox(folder, "deleted")
    result = DeleteResult()
    if await _has_contents(session, folder.id):
        if not move_contents_to_parent:
            raise FolderNotEmpty(
                f'"{await path_of(session, folder.id)}" is not empty; '
                "move its contents to the parent folder to delete it"
            )
        files_dest = folder.parent_id or await inbox_id(session)
        # A subfolder may have the same name as the folder being deleted; free the name first.
        folder.name = f"~deleting-{uuid.uuid4().hex}"
        await session.flush()
        await _merge(session, folder, folder.parent_id, files_dest, result)
    await session.delete(folder)
    await session.flush()
    return result
