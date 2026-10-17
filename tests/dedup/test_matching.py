"""Perceptual matching end to end: ingest, rehash, recompute-similar (section 8)."""

from pathlib import Path

import psycopg
import pytest

from app.db.models_files import IngestOutcome
from app.dedup import service
from app.dedup.hashing import to_unsigned
from app.dedup.settings import DedupSettings, get_dedup_settings
from app.ingest.process import retry_file
from tests.ingest.helpers import one, run
from tests.media import (
    make_audio,
    make_lavfi_video,
    reencode_video,
    structured_png,
    variant_jpeg,
    variant_resized,
    variant_webp,
)

pytestmark = pytest.mark.usefixtures("clean_ingest")


def file_id(conn: psycopg.Connection, name: str) -> int:
    return one(conn, "SELECT id FROM files WHERE original_filename = %s", (name,))[0]


def pairs(conn: psycopg.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT file_a_id, file_b_id, method::text, distance, dismissed FROM similar_pairs"
        " ORDER BY file_a_id, file_b_id, method"
    ).fetchall()


def pair_ids(conn: psycopg.Connection) -> set[tuple[int, int]]:
    return {(a, b) for a, b, *_ in pairs(conn)}


def test_settings_come_from_config(settings) -> None:
    assert get_dedup_settings(settings) == DedupSettings(6, 8)
    assert get_dedup_settings() == DedupSettings(6, 8)


async def test_image_variants_are_flagged(sessionmaker, paths, db_conn) -> None:
    original = structured_png(1)
    variants = {
        "orig.png": original,
        "half.png": variant_resized(original),
        "q60.jpg": variant_jpeg(original),
        "conv.webp": variant_webp(original),
    }
    for name, data in variants.items():
        outcome, _ = await run(sessionmaker, paths, data, name)
        assert outcome is IngestOutcome.new

    ids = [file_id(db_conn, n) for n in variants]
    (phash,) = one(db_conn, "SELECT phash FROM files WHERE id = %s", (ids[0],))
    assert phash is not None and 0 <= to_unsigned(phash) < 2**64
    # Every variant is similar to every other one: 4 files -> 6 pairs.
    found = pairs(db_conn)
    assert {(a, b) for a, b, *_ in found} == {
        (ids[i], ids[j]) for i in range(4) for j in range(i + 1, 4)
    }
    assert all(m == "image_phash" and d <= 6 and not dis for _, _, m, d, dis in found)
    assert all(a < b for a, b, *_ in found)


async def test_unrelated_images_are_not_similar(sessionmaker, paths, db_conn) -> None:
    for seed in range(200, 205):
        await run(sessionmaker, paths, structured_png(seed), f"u{seed}.png")
    assert one(db_conn, "SELECT count(*) FROM files WHERE phash IS NOT NULL") == (5,)
    assert pairs(db_conn) == []


async def test_variant_of_discarded_file_is_flagged(sessionmaker, paths, db_conn) -> None:
    original = structured_png(2)
    await run(sessionmaker, paths, original, "first.png")
    first = file_id(db_conn, "first.png")
    db_conn.execute(
        "UPDATE files SET status = 'discarded', discarded_at = now() WHERE id = %s", (first,)
    )
    await run(sessionmaker, paths, variant_jpeg(original), "again.jpg")
    again = file_id(db_conn, "again.jpg")
    assert pair_ids(db_conn) == {(first, again)}

    # An exact copy is still dropped before any perceptual work.
    outcome, _ = await run(sessionmaker, paths, original, "copy.png")
    assert outcome is IngestOutcome.duplicate_exact
    assert one(db_conn, "SELECT count(*) FROM files") == (2,)


async def test_videos(sessionmaker, paths, db_conn, tmp_path: Path) -> None:
    src = make_lavfi_video(tmp_path / "src.mp4", "testsrc2", seconds=4)
    small = reencode_video(src, tmp_path / "small.mp4", size="160x120", bitrate="100k")
    mandel = make_lavfi_video(tmp_path / "mandel.mp4", "mandelbrot", seconds=4)
    long = make_lavfi_video(tmp_path / "long.mp4", "testsrc2", seconds=12)
    for path in (src, small, mandel, long):
        outcome, _ = await run(sessionmaker, paths, path.read_bytes(), path.name)
        assert outcome is IngestOutcome.new

    ids = {p.name: file_id(db_conn, p.name) for p in (src, small, mandel, long)}
    frames = db_conn.execute(
        "SELECT file_id, count(*) FROM video_frame_hashes GROUP BY file_id"
    ).fetchall()
    assert dict(frames) == {i: 5 for i in ids.values()}
    assert one(db_conn, "SELECT count(*) FROM files WHERE phash IS NOT NULL") == (0,)

    found = pairs(db_conn)
    assert [(a, b, m) for a, b, m, _, _ in found] == [
        (ids["src.mp4"], ids["small.mp4"], "video_frames")
    ]
    assert found[0][3] <= 8

    async with sessionmaker() as session:
        # A 12 s video is never a candidate for a 4 s one; the 4 s mandelbrot is but fails.
        candidates = await service.video_candidates(session, ids["src.mp4"], 4000)
    assert ids["long.mp4"] not in candidates
    assert ids["mandel.mp4"] in candidates


async def test_short_videos_hash_one_frame(sessionmaker, paths, db_conn, tmp_path) -> None:
    a = make_lavfi_video(tmp_path / "a.mp4", seconds=0.6)
    b = reencode_video(a, tmp_path / "b.mp4")
    for path in (a, b):
        await run(sessionmaker, paths, path.read_bytes(), path.name)
    rows = db_conn.execute("SELECT DISTINCT frame_index FROM video_frame_hashes").fetchall()
    assert rows == [(0,)]
    assert pair_ids(db_conn) == {(file_id(db_conn, "a.mp4"), file_id(db_conn, "b.mp4"))}


def test_duration_rule() -> None:
    low, high = service.duration_bounds(10_000)
    assert (low, high) == (9_000, 11_000)
    low, high = service.duration_bounds(100_000)
    assert low == pytest.approx(97_000) and high == pytest.approx(100_000 / 0.97)


def test_compare_frames() -> None:
    a = {i: 0 for i in range(5)}
    near = {0: 0b1, 1: 0b11, 2: 2**64 - 1, 3: 2**64 - 1, 4: 0b111}
    assert service.compare_frames(a, near, 8) == 3  # 3 of 5 match; median of 1,2,64,64,3
    two = {0: 0, 1: 0, 2: 2**64 - 1, 3: 2**64 - 1, 4: 2**64 - 1}
    assert service.compare_frames(a, two, 8) is None
    assert service.compare_frames({0: 0}, {0: 0b1111}, 8) == 4
    assert service.compare_frames({0: 0}, {0: 2**16 - 1}, 8) is None
    assert service.compare_frames({0: 0}, {1: 0}, 8) is None


async def test_audio_and_other_files_have_no_hash(sessionmaker, paths, db_conn, tmp_path) -> None:
    await run(sessionmaker, paths, make_audio(tmp_path / "t.mp3").read_bytes(), "t.mp3")
    await run(sessionmaker, paths, b"plain text", "notes.txt")
    assert one(db_conn, "SELECT count(*) FROM files WHERE phash IS NOT NULL") == (0,)
    assert one(db_conn, "SELECT count(*) FROM video_frame_hashes") == (0,)


async def test_hash_failure_keeps_file_reviewable(
    sessionmaker, paths, db_conn, monkeypatch
) -> None:
    def broken(path):
        raise OSError("decoder exploded")

    monkeypatch.setattr(service, "phash_image_file", broken)
    outcome, _ = await run(sessionmaker, paths, structured_png(9), "x.png")
    assert outcome is IngestOutcome.new
    status, phash, meta = one(db_conn, "SELECT status::text, phash, metadata FROM files")
    assert (status, phash) == ("pending_review", None)
    assert "decoder exploded" in meta["perceptual_error"]


async def _two_similar_and_dismiss(sessionmaker, paths, db_conn) -> tuple[int, int]:
    original = structured_png(3)
    await run(sessionmaker, paths, original, "a.png")
    await run(sessionmaker, paths, variant_resized(original), "b.png")
    a, b = file_id(db_conn, "a.png"), file_id(db_conn, "b.png")
    async with sessionmaker() as session:
        assert await service.dismiss_pair(session, b, a, service.SimilarMethod.image_phash)
        await session.commit()
    assert pairs(db_conn)[0][4] is True
    return a, b


async def test_dismissed_pair_survives_ingest_rehash_and_recompute(
    sessionmaker, paths, db_conn
) -> None:
    a, b = await _two_similar_and_dismiss(sessionmaker, paths, db_conn)

    # Ingest path again: the retried file recomputes its hashes and matches.
    db_conn.execute("UPDATE files SET status = 'error' WHERE id = %s", (b,))
    assert await retry_file(sessionmaker, paths, b) is IngestOutcome.new
    assert pairs(db_conn) == [(a, b, "image_phash", pairs(db_conn)[0][3], True)]

    stats = await service.rehash(sessionmaker, paths)
    assert (stats.hashed, stats.errors) == (2, 0)
    assert [p[4] for p in pairs(db_conn)] == [True]

    stats = await service.recompute_similar(sessionmaker)
    assert stats.as_dict() == {"pairs": 1, "inserted": 0, "removed": 0, "dismissed_kept": 1}
    assert [p[4] for p in pairs(db_conn)] == [True]


async def test_recompute_applies_current_thresholds(sessionmaker, paths, db_conn) -> None:
    original = structured_png(4)
    await run(sessionmaker, paths, original, "a.png")
    await run(sessionmaker, paths, variant_jpeg(original), "b.jpg")
    await run(sessionmaker, paths, structured_png(5), "c.png")
    a, b, c = (file_id(db_conn, n) for n in ("a.png", "b.jpg", "c.png"))
    assert pair_ids(db_conn) == {(a, b)}
    (distance,) = one(db_conn, "SELECT distance FROM similar_pairs")

    # A pair lost to a stricter threshold is deleted; looser thresholds add pairs.
    db_conn.execute(
        "UPDATE files SET phash = phash # 1023 WHERE id = %s", (b,)
    )  # flip 10 bits: distance grows past 6
    stats = await service.recompute_similar(sessionmaker)
    assert stats.removed == 1 and pair_ids(db_conn) == set()

    stats = await service.recompute_similar(sessionmaker, DedupSettings(64, 8))
    assert stats.inserted == 3 and pair_ids(db_conn) == {(a, b), (a, c), (b, c)}
    assert distance <= 6


async def test_rehash_missing_only_and_missing_original(sessionmaker, paths, db_conn) -> None:
    original = structured_png(6)
    await run(sessionmaker, paths, original, "a.png")
    _, rel_b = await run(sessionmaker, paths, variant_webp(original), "b.webp")
    a, b = file_id(db_conn, "a.png"), file_id(db_conn, "b.webp")
    db_conn.execute("UPDATE files SET phash = NULL")
    db_conn.execute("DELETE FROM similar_pairs")
    paths.resolve(rel_b).unlink()  # b lost its original

    stats = await service.rehash(sessionmaker, paths, missing_only=True)
    assert (stats.considered, stats.hashed, stats.no_original) == (2, 1, 1)
    assert one(db_conn, "SELECT phash IS NOT NULL FROM files WHERE id = %s", (a,)) == (True,)

    stats = await service.rehash(sessionmaker, paths, missing_only=True)
    assert (stats.considered, stats.hashed, stats.no_original) == (1, 0, 1)
    assert pair_ids(db_conn) == set()
    assert one(db_conn, "SELECT phash FROM files WHERE id = %s", (b,)) == (None,)
