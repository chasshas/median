import asyncio
import hashlib
from pathlib import Path

import psycopg
import pytest
from PIL import Image

from app.db.models_files import IngestOutcome
from app.ingest import process as process_module
from app.ingest.origin import IngestOrigin
from app.ingest.process import process_ingest, retry_file
from app.storage import DataPaths
from tests.ingest.helpers import events, one, run, stage
from tests.media import (
    gif_animated_bytes,
    jpeg_bytes,
    make_audio,
    make_audio_with_cover,
    make_video,
    png_bytes,
    write,
)

pytestmark = pytest.mark.usefixtures("clean_ingest")


async def test_new_image(sessionmaker, paths: DataPaths, db_conn: psycopg.Connection) -> None:
    data = png_bytes()
    outcome, rel = await run(sessionmaker, paths, data, "red.png")

    assert outcome is IngestOutcome.new
    assert one(db_conn, "SELECT count(*) FROM files") == (1,)
    status, kind, w, h, has_thumb, staging, sha, ext, mime, name = one(
        db_conn,
        "SELECT status::text, media_kind::text, width, height, has_thumb, staging_path, sha256,"
        " ext, mime, original_filename FROM files",
    )
    assert (status, kind, w, h, has_thumb) == ("pending_review", "image", 64, 48, True)
    assert (ext, mime, name) == ("png", "image/png", "red.png")
    assert staging == rel and paths.resolve(rel).is_file()
    assert bytes(sha) == hashlib.sha256(data).digest()
    thumb = paths.thumb_path(bytes(sha).hex())
    with Image.open(thumb) as img:
        assert img.format == "WEBP"
        assert max(img.size) <= 320
    assert events(db_conn) == ["new"]
    assert one(db_conn, "SELECT source_key, remote_id FROM file_sources") == ("upload", None)


async def test_exif_orientation_and_thumb_size(
    sessionmaker, paths: DataPaths, db_conn: psycopg.Connection
) -> None:
    # Orientation 6 = rotate 90 degrees: an 800x400 sensor image displays as 400x800.
    await run(sessionmaker, paths, jpeg_bytes(orientation=6), "rotated.jpg")
    (sha,) = one(db_conn, "SELECT sha256 FROM files")
    with Image.open(paths.thumb_path(bytes(sha).hex())) as img:
        assert img.size == (160, 320)


async def test_animated_gif_is_image(sessionmaker, paths, db_conn) -> None:
    await run(sessionmaker, paths, gif_animated_bytes(), "anim.gif")
    kind, meta, has_thumb = one(db_conn, "SELECT media_kind::text, metadata, has_thumb FROM files")
    assert kind == "image" and has_thumb
    assert meta["animated"] is True and meta["frames"] == 3


async def test_same_bytes_twice(sessionmaker, paths, db_conn) -> None:
    data = png_bytes()
    await run(sessionmaker, paths, data, "a.png")
    outcome, rel = await run(sessionmaker, paths, data, "b.png")

    assert outcome is IngestOutcome.duplicate_exact
    assert one(db_conn, "SELECT count(*) FROM files") == (1,)
    assert events(db_conn) == ["new", "duplicate_exact"]
    assert not paths.resolve(rel).exists()
    assert one(db_conn, "SELECT count(*) FROM file_sources") == (1,)


async def test_duplicate_of_discarded_is_dropped(sessionmaker, paths, db_conn) -> None:
    data = png_bytes((1, 2, 3))
    await run(sessionmaker, paths, data, "a.png")
    db_conn.execute("UPDATE files SET status = 'discarded', discarded_at = now()")

    outcome, rel = await run(sessionmaker, paths, data, "again.png")

    assert outcome is IngestOutcome.duplicate_exact
    assert one(db_conn, "SELECT status::text FROM files") == ("discarded",)
    assert not paths.resolve(rel).exists()


async def test_force_restores_discarded(sessionmaker, paths, db_conn) -> None:
    data = png_bytes((4, 5, 6))
    _, first = await run(sessionmaker, paths, data, "a.png")
    (sha, ext) = one(db_conn, "SELECT sha256, ext FROM files")
    # Simulate a discard: the original sits in trash/.
    trash = paths.trash_path(bytes(sha).hex(), ext)
    trash.parent.mkdir(parents=True, exist_ok=True)
    paths.resolve(first).rename(trash)
    db_conn.execute("UPDATE files SET status = 'discarded', discarded_at = now()")

    outcome, rel = await run(sessionmaker, paths, data, "again.png", IngestOrigin(force=True))

    assert outcome is IngestOutcome.forced_reingest
    status, staging, discarded_at = one(
        db_conn, "SELECT status::text, staging_path, discarded_at FROM files"
    )
    assert (status, staging, discarded_at) == ("pending_review", rel, None)
    assert paths.resolve(rel).is_file()
    assert not trash.exists()
    assert events(db_conn)[-1] == "forced_reingest"


async def test_force_on_live_file_is_still_duplicate(sessionmaker, paths, db_conn) -> None:
    data = png_bytes((7, 8, 9))
    await run(sessionmaker, paths, data, "a.png")
    outcome, _ = await run(sessionmaker, paths, data, "b.png", IngestOrigin(force=True))
    assert outcome is IngestOutcome.duplicate_exact


async def test_known_remote_skipped_before_hashing(
    sessionmaker, paths, db_conn, monkeypatch
) -> None:
    origin = IngestOrigin(source_key="site", remote_id="42", url="https://example.test/42")
    await run(sessionmaker, paths, png_bytes((10, 0, 0)), "a.png", origin)

    def no_hashing(path: Path) -> bytes:
        raise AssertionError("hashed a known remote item")

    monkeypatch.setattr(process_module, "sha256_file", no_hashing)
    outcome, rel = await run(sessionmaker, paths, png_bytes((20, 0, 0)), "b.png", origin)

    assert outcome is IngestOutcome.skipped_known_remote
    assert not paths.resolve(rel).exists()
    assert one(db_conn, "SELECT count(*) FROM files") == (1,)
    remote = one(
        db_conn, "SELECT source_key, remote_id, file_id FROM ingest_events ORDER BY id DESC LIMIT 1"
    )
    assert remote == ("site", "42", None)


async def test_crawler_tag_merge_on_duplicate(sessionmaker, paths, db_conn) -> None:
    data = png_bytes((30, 0, 0))
    await run(
        sessionmaker, paths, data, "a.png", IngestOrigin(source_key="site", tags=["Keep", "gone"])
    )
    db_conn.execute(
        "UPDATE file_tags SET removed = true"
        " WHERE tag_id = (SELECT id FROM tags WHERE name = 'gone')"
    )

    outcome, _ = await run(
        sessionmaker,
        paths,
        data,
        "b.png",
        IngestOrigin(source_key="other", remote_id="x1", tags=["gone", "New Tag"]),
    )

    assert outcome is IngestOutcome.duplicate_exact
    rows = db_conn.execute(
        "SELECT t.name, ft.origin::text, ft.removed FROM file_tags ft"
        " JOIN tags t ON t.id = ft.tag_id ORDER BY t.name"
    ).fetchall()
    assert rows == [
        ("gone", "crawler", True),
        ("keep", "crawler", False),
        ("new_tag", "crawler", False),
    ]
    sources = db_conn.execute(
        "SELECT source_key, remote_id FROM file_sources ORDER BY id"
    ).fetchall()
    assert sources == [("site", None), ("other", "x1")]


async def test_user_tags_from_upload(sessionmaker, paths, db_conn) -> None:
    data = png_bytes((40, 0, 0))
    await run(sessionmaker, paths, data, "a.png", IngestOrigin(tags=["mine"], tag_origin="user"))
    db_conn.execute("UPDATE file_tags SET removed = true")
    await run(sessionmaker, paths, data, "b.png", IngestOrigin(tags=["mine"], tag_origin="user"))
    row = one(db_conn, "SELECT ft.origin::text, ft.removed FROM file_tags ft")
    assert row == ("user", False)


async def test_video(sessionmaker, paths, db_conn, tmp_path) -> None:
    video = make_video(tmp_path / "clip.mp4", seconds=2.0)
    outcome, _ = await run(sessionmaker, paths, video.read_bytes(), "clip.mp4")

    assert outcome is IngestOutcome.new
    kind, w, h, duration, has_thumb, ext, mime, meta, sha = one(
        db_conn,
        "SELECT media_kind::text, width, height, duration_ms, has_thumb, ext, mime, metadata,"
        " sha256 FROM files",
    )
    assert (kind, w, h, ext, mime) == ("video", 320, 240, "mp4", "video/mp4")
    assert 1900 <= duration <= 2100
    assert meta["video_codec"] == "h264"
    assert has_thumb and paths.thumb_path(bytes(sha).hex()).is_file()


async def test_audio_without_cover(sessionmaker, paths, db_conn, tmp_path) -> None:
    audio = make_audio(tmp_path / "tone.wav")
    await run(sessionmaker, paths, audio.read_bytes(), "tone.wav")
    kind, duration, has_thumb = one(
        db_conn, "SELECT media_kind::text, duration_ms, has_thumb FROM files"
    )
    assert kind == "audio" and 900 <= duration <= 1100 and has_thumb is False


async def test_audio_cover_art_thumbnail(sessionmaker, paths, db_conn, tmp_path) -> None:
    cover = write(tmp_path / "cover.png", png_bytes((0, 200, 0), size=(500, 500)))
    audio = make_audio_with_cover(tmp_path / "song.mp3", cover)
    await run(sessionmaker, paths, audio.read_bytes(), "song.mp3")
    kind, has_thumb, meta, sha = one(
        db_conn, "SELECT media_kind::text, has_thumb, metadata, sha256 FROM files"
    )
    assert (kind, has_thumb, meta["cover_art"]) == ("audio", True, True)
    with Image.open(paths.thumb_path(bytes(sha).hex())) as img:
        assert img.size == (320, 320)


async def test_corrupt_jpeg_is_other(sessionmaker, paths, db_conn) -> None:
    outcome, _ = await run(sessionmaker, paths, b"\xff\xd8\xff\xe0 not really a jpeg", "bad.jpg")
    assert outcome is IngestOutcome.new
    status, kind, meta, has_thumb, ext = one(
        db_conn, "SELECT status::text, media_kind::text, metadata, has_thumb, ext FROM files"
    )
    assert (status, kind, has_thumb, ext) == ("pending_review", "other", False, "jpg")
    assert "could not be decoded" in meta["probe_error"]


async def test_plain_file_is_other_without_probe_error(sessionmaker, paths, db_conn) -> None:
    await run(sessionmaker, paths, b"hello world\n", "notes.txt")
    kind, meta, mime = one(db_conn, "SELECT media_kind::text, metadata, mime FROM files")
    assert kind == "other" and "probe_error" not in meta and mime == "text/plain"


async def test_unexpected_exception_then_retry(sessionmaker, paths, db_conn, monkeypatch) -> None:
    async def broken_probe(path, name):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(process_module, "probe", broken_probe)
    outcome, rel = await run(sessionmaker, paths, png_bytes((50, 0, 0)), "a.png")

    assert outcome is IngestOutcome.error
    file_id, status, message = one(db_conn, "SELECT id, status::text, error_message FROM files")
    assert status == "error" and "probe exploded" in message
    assert paths.resolve(rel).is_file()
    assert events(db_conn) == ["error"]

    monkeypatch.undo()
    db_conn.execute("UPDATE files SET status = 'processing' WHERE id = %s", (file_id,))
    assert await retry_file(sessionmaker, paths, file_id) is IngestOutcome.new
    assert one(db_conn, "SELECT status::text, error_message FROM files") == ("pending_review", None)
    batch = one(db_conn, "SELECT batch_id FROM ingest_events ORDER BY id DESC LIMIT 1")
    assert batch == (rel.split("/")[1],)


async def test_concurrent_identical_content(sessionmaker, paths, db_conn) -> None:
    data = png_bytes((60, 0, 0), size=(300, 300))
    rel_a = stage(paths, data, "a.png")
    rel_b = stage(paths, data, "b.png")

    outcomes = await asyncio.gather(
        process_ingest(sessionmaker, paths, rel_a, IngestOrigin(), "a.png"),
        process_ingest(sessionmaker, paths, rel_b, IngestOrigin(), "b.png"),
    )

    assert sorted(o.value for o in outcomes) == ["duplicate_exact", "new"]
    assert one(db_conn, "SELECT count(*) FROM files") == (1,)
    assert sorted(events(db_conn)) == ["duplicate_exact", "new"]
    (staging,) = one(db_conn, "SELECT staging_path FROM files")
    assert paths.resolve(staging).is_file()
    assert sum(paths.resolve(r).exists() for r in (rel_a, rel_b)) == 1
