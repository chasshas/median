"""Folder lookups needed before phase 4 builds folder management (ARCHITECTURE.md section 10)."""

from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_library import INBOX_NAME, Folder


@dataclass(frozen=True)
class FolderChoice:
    id: int
    path: str  # "A / B / C"


async def inbox_id(session: AsyncSession) -> int:
    found = await session.scalar(
        select(Folder.id).where(Folder.parent_id.is_(None), Folder.name == INBOX_NAME)
    )
    if found is None:
        raise RuntimeError("the Inbox folder is missing; run make migrate")
    return found


async def folder_exists(session: AsyncSession, folder_id: int) -> bool:
    return await session.get(Folder, folder_id) is not None


async def folder_choices(session: AsyncSession) -> list[FolderChoice]:
    """Every folder with its display path, Inbox first, then by path."""
    rows = await session.execute(
        text(
            "WITH RECURSIVE tree(id, path, top) AS ("
            "  SELECT id, name, name FROM folders WHERE parent_id IS NULL"
            "  UNION ALL"
            "  SELECT f.id, tree.path || ' / ' || f.name, tree.top"
            "  FROM folders f JOIN tree ON f.parent_id = tree.id"
            ") SELECT id, path FROM tree"
            " ORDER BY (path = :inbox) DESC, lower(path)"
        ),
        {"inbox": INBOX_NAME},
    )
    return [FolderChoice(folder_id, path) for folder_id, path in rows.all()]
