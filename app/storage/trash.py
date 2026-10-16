"""Trash: originals of discarded files, kept for undo until purged."""

from pathlib import Path

from app.storage.fs import atomic_move
from app.storage.paths import DataPaths


def move_to_trash(paths: DataPaths, src: Path, sha_hex: str, ext: str | None) -> Path:
    return atomic_move(src, paths.trash_path(sha_hex, ext))


def restore_from_trash(paths: DataPaths, sha_hex: str, ext: str | None, dest: Path) -> Path:
    """Move a trashed original back to dest. Raises FileNotFoundError once it was purged."""
    src = paths.trash_path(sha_hex, ext)
    if not src.exists():
        raise FileNotFoundError(src)
    return atomic_move(src, dest)


def in_trash(paths: DataPaths, sha_hex: str, ext: str | None) -> bool:
    return paths.trash_path(sha_hex, ext).exists()
