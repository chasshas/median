"""Atomic file operations and the after-commit hook."""

import contextlib
import logging
import os
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_AFTER_COMMIT_KEY = "median_after_commit"
_COPY_CHUNK = 1024 * 1024


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _temp_name(dest: Path) -> Path:
    return dest.parent / f".{dest.name}.{uuid.uuid4().hex[:8]}.tmp"


def _fsync_file(path: Path) -> None:
    with open(path, "rb") as fh:
        os.fsync(fh.fileno())


def _copy_into_temp(src: Path, tmp: Path) -> None:
    with open(src, "rb") as fin, open(tmp, "xb") as fout:
        shutil.copyfileobj(fin, fout, _COPY_CHUNK)
        fout.flush()
        os.fsync(fout.fileno())


def atomic_move(src: Path, dest: Path) -> Path:
    """Move src to dest atomically: temp name in dest's directory, fsync, os.replace.

    Works across filesystems (copy then delete). dest is replaced if it exists.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_name(dest)
    try:
        if os.stat(src).st_dev == os.stat(dest.parent).st_dev:
            os.rename(src, tmp)
            _fsync_file(tmp)
        else:
            _copy_into_temp(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        if tmp.exists() and not src.exists():
            os.rename(tmp, src)  # put a renamed file back where it was
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    _fsync_dir(dest.parent)
    with contextlib.suppress(FileNotFoundError):
        src.unlink()  # cross-filesystem case
    return dest


def atomic_copy(src: Path, dest: Path) -> Path:
    """Copy src to dest atomically (temp name in dest's directory, fsync, os.replace)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_name(dest)
    try:
        _copy_into_temp(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    _fsync_dir(dest.parent)
    return dest


def atomic_write_bytes(dest: Path, data: bytes) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_name(dest)
    try:
        with open(tmp, "xb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    _fsync_dir(dest.parent)
    return dest


def write_private_file(dest: Path, data: bytes) -> Path:
    """Write a secret atomically: directory mode 0700, file mode 0600 from the start."""
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(dest.parent, 0o700)
    tmp = _temp_name(dest)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)  # in case the umask removed bits we did not ask for
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    _fsync_dir(dest.parent)
    return dest


def remove_tree(path: Path) -> None:
    """Delete a directory tree; a missing directory is fine."""
    shutil.rmtree(path, ignore_errors=True)


def fresh_dir(path: Path) -> Path:
    """An empty directory at path (an existing one is emptied first)."""
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    return path


def unlink_quietly(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


# After-commit hook --------------------------------------------------------------------------


def run_after_commit(session: AsyncSession | Session, action: Callable[[], object]) -> None:
    """Run action (a filesystem change) once the session's current transaction commits.

    If the transaction rolls back, the action is dropped. Actions run in registration order;
    a failing action is logged and the remaining ones still run.
    """
    sync = session.sync_session if isinstance(session, AsyncSession) else session
    sync.info.setdefault(_AFTER_COMMIT_KEY, []).append(action)


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    actions = session.info.pop(_AFTER_COMMIT_KEY, [])
    for action in actions:
        try:
            action()
        except Exception:
            logger.exception("after-commit filesystem action failed: %r", action)


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop(_AFTER_COMMIT_KEY, None)
