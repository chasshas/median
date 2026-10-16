from pathlib import Path

import pytest
from sqlalchemy import text

from app.storage import DataPaths, atomic_copy, atomic_move, atomic_write_bytes, run_after_commit
from app.storage.paths import safe_filename
from app.storage.trash import in_trash, move_to_trash, restore_from_trash

SHA = "ab" + "cd" + "0" * 60


def test_layout(tmp_path: Path) -> None:
    p = DataPaths(tmp_path)
    assert p.archive_path(SHA, "jpg") == tmp_path / "archive/ab/cd" / f"{SHA}.jpg"
    assert p.thumb_path(SHA) == tmp_path / "thumbs/ab" / f"{SHA}.webp"
    assert p.trash_path(SHA, "mp4") == tmp_path / "trash" / f"{SHA}.mp4"
    assert p.staging_batch("run-12") == tmp_path / "staging/run-12"
    assert p.work_dir(5) == tmp_path / "work/5"
    assert p.addon_state_dir(3) == tmp_path / "addon_state/3"
    assert p.job_log(7) == tmp_path / "logs/jobs/7.log"
    assert p.tools_venv == tmp_path / "tools/venv"
    assert p.secrets == tmp_path / "secrets"
    assert p.backups == tmp_path / "backups"


def test_staging_batch_rejects_bad_ids(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        DataPaths(tmp_path).staging_batch("../etc")


def test_resolve_refuses_escape(tmp_path: Path) -> None:
    p = DataPaths(tmp_path)
    assert p.resolve("staging/x/a.jpg") == (tmp_path / "staging/x/a.jpg").resolve()
    with pytest.raises(ValueError):
        p.resolve("../outside")


def test_original_path_by_status(tmp_path: Path) -> None:
    p = DataPaths(tmp_path)
    assert p.original_path("archived", None, SHA, "jpg") == p.archive_path(SHA, "jpg")
    assert p.original_path("discarded", "staging/x/a", SHA, "jpg") == p.trash_path(SHA, "jpg")
    assert p.original_path("pending_review", "staging/x/a", SHA, "jpg") == p.resolve("staging/x/a")


@pytest.mark.parametrize(
    ("name", "expected"),
    [("photo.jpg", "photo.jpg"), ("../../etc/passwd", "passwd"), ("a\\b.png", "b.png"),
     ("..", "file"), (".hidden", "hidden"), ("x y?.gif", "x y_.gif")],
)  # fmt: skip
def test_safe_filename(name: str, expected: str) -> None:
    assert safe_filename(name) == expected


def test_atomic_move_and_copy(tmp_path: Path) -> None:
    src = tmp_path / "in.bin"
    src.write_bytes(b"data")
    dest = tmp_path / "deep/dir/out.bin"
    atomic_move(src, dest)
    assert not src.exists()
    assert dest.read_bytes() == b"data"
    copy = atomic_copy(dest, tmp_path / "copy/out.bin")
    assert copy.read_bytes() == b"data" and dest.exists()
    atomic_write_bytes(dest, b"new")
    assert dest.read_bytes() == b"new"
    assert not list(dest.parent.glob(".*.tmp"))


def test_atomic_move_missing_source(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        atomic_move(tmp_path / "missing", tmp_path / "out")


def test_trash_roundtrip(tmp_path: Path) -> None:
    p = DataPaths(tmp_path)
    src = tmp_path / "staging/b/file.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"jpg")
    move_to_trash(p, src, SHA, "jpg")
    assert in_trash(p, SHA, "jpg") and not src.exists()
    restored = restore_from_trash(p, SHA, "jpg", p.archive_path(SHA, "jpg"))
    assert restored.read_bytes() == b"jpg"
    assert not in_trash(p, SHA, "jpg")
    with pytest.raises(FileNotFoundError):
        restore_from_trash(p, SHA, "jpg", tmp_path / "x")


async def test_after_commit_runs_only_on_commit(sessionmaker) -> None:
    ran: list[str] = []
    async with sessionmaker() as session:
        await session.execute(text("SELECT 1"))
        run_after_commit(session, lambda: ran.append("rolled back"))
        await session.rollback()
        await session.execute(text("SELECT 1"))
        run_after_commit(session, lambda: ran.append("committed"))
        assert ran == []
        await session.commit()
    assert ran == ["committed"]


async def test_after_commit_failure_does_not_stop_others(sessionmaker) -> None:
    ran: list[str] = []

    def boom() -> None:
        raise OSError("disk")

    async with sessionmaker() as session:
        await session.execute(text("SELECT 1"))
        run_after_commit(session, boom)
        run_after_commit(session, lambda: ran.append("second"))
        await session.commit()
    assert ran == ["second"]
