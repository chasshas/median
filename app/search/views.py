"""Search results for the API and the /search page: files with folder paths and active tags."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_files import File, FileStatus
from app.db.models_library import FileTag, Tag
from app.library.folders import folder_choices
from app.search.filters import SearchPage
from app.storage import get_paths


@dataclass
class ResultItem:
    file: File
    folder_path: str | None
    tags: list[str]
    original_deleted: bool  # discarded and the trash copy is purged

    def as_json(self) -> dict[str, object]:
        f = self.file
        return {
            "id": f.id,
            "title": f.title,
            "original_filename": f.original_filename,
            "status": f.status.value,
            "media_kind": f.media_kind.value,
            "size_bytes": f.size_bytes,
            "width": f.width,
            "height": f.height,
            "folder_id": f.folder_id,
            "folder_path": self.folder_path,
            "tags": self.tags,
            "collected_at": f.collected_at.isoformat(),
            "archived_at": f.archived_at.isoformat() if f.archived_at else None,
            "thumb_url": f"/media/{f.id}/thumb" if f.has_thumb else None,
            "original_url": None if self.original_deleted else f"/media/{f.id}/original",
            "original_deleted": self.original_deleted,
        }


async def result_items(session: AsyncSession, page: SearchPage) -> list[ResultItem]:
    ids = [f.id for f in page.files]
    tags: dict[int, list[str]] = {i: [] for i in ids}
    if ids:
        rows = await session.execute(
            select(FileTag.file_id, Tag.name)
            .join(Tag, Tag.id == FileTag.tag_id)
            .where(FileTag.file_id.in_(ids), FileTag.removed.is_(False))
            .order_by(Tag.name)
        )
        for file_id, name in rows.all():
            tags[file_id].append(name)
    paths = {c.id: c.path for c in await folder_choices(session)} if ids else {}
    data = get_paths()
    return [
        ResultItem(
            f,
            paths.get(f.folder_id) if f.folder_id is not None else None,
            tags[f.id],
            f.status is FileStatus.discarded and not data.trash_path(f.sha_hex, f.ext).exists(),
        )
        for f in page.files
    ]
