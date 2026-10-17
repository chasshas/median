import io
import random
from pathlib import Path

import imagehash
import pytest
from PIL import Image

from app.dedup.hashing import (
    hamming,
    hash_to_int,
    phash_image,
    phash_png_bytes,
    to_signed,
    to_unsigned,
    video_frame_hashes,
    video_frame_times,
)
from tests.media import (
    encode,
    make_lavfi_video,
    structured_image,
    structured_png,
    variant_jpeg,
    variant_resized,
    variant_webp,
)


def reference_signed(u: int) -> int:
    """Two's complement by the definition, independent of the implementation."""
    return int.from_bytes(u.to_bytes(8, "big", signed=False), "big", signed=True)


def reference_popcount(x: int) -> int:
    return bin(x).count("1")


EDGE_VALUES = [0, 1, 2**63 - 1, 2**63, 2**63 + 1, 2**64 - 1, 0xAAAAAAAAAAAAAAAA]


def random_values(n: int = 2000) -> list[int]:
    rng = random.Random(1234)
    values = [rng.getrandbits(64) for _ in range(n)]
    values += [v | (1 << 63) for v in values[:200]]  # make sure the high bit is covered
    return values + EDGE_VALUES


def test_signed_round_trip_matches_reference() -> None:
    for u in random_values():
        s = to_signed(u)
        assert s == reference_signed(u)
        assert -(2**63) <= s < 2**63
        assert to_unsigned(s) == u
    assert to_signed(2**63) == -(2**63)
    assert to_signed(2**64 - 1) == -1


def test_signed_conversion_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        to_signed(-1)
    with pytest.raises(ValueError):
        to_signed(2**64)
    with pytest.raises(ValueError):
        to_unsigned(2**63)


def test_hamming_matches_reference() -> None:
    values = random_values(500)
    for a, b in zip(values, reversed(values), strict=True):
        assert hamming(a, b) == reference_popcount(a ^ b)


def test_hash_to_int_matches_imagehash_hex() -> None:
    for seed in range(5):
        h = imagehash.phash(structured_image(seed), hash_size=8)
        assert hash_to_int(h) == int(str(h), 16)
        assert hamming(hash_to_int(h), 0) == int(h.hash.sum())


def test_phash_uses_exif_transposed_rgb_first_frame() -> None:
    img = structured_image(3)
    # Pixels stored turned 90 degrees counter-clockwise; orientation 6 tells viewers to turn
    # them back clockwise, so the displayed image is img.
    stored = img.rotate(90, expand=True)
    tagged = encode(stored, "JPEG", quality=95, exif=_orientation(6))
    upright = phash_png_bytes(encode(img, "PNG"))
    assert hamming(phash_png_bytes(tagged), upright) <= 2
    assert hamming(phash_image(stored), upright) > 6  # without the transpose it would not match

    # Animated images use the first frame; RGBA and palette images are hashed as RGB.
    first, second = structured_image(4, (200, 150)), structured_image(5, (200, 150))
    gif = io.BytesIO()
    first.save(gif, "GIF", save_all=True, append_images=[second], duration=100)
    assert hamming(phash_png_bytes(gif.getvalue()), phash_image(first)) <= 4
    assert hamming(phash_png_bytes(gif.getvalue()), phash_image(second)) > 6
    assert phash_image(first.convert("RGBA")) == phash_image(first)


def _orientation(value: int) -> bytes:
    exif = Image.Exif()
    exif[0x0112] = value
    return exif.tobytes()


def test_variants_are_close_and_unrelated_far() -> None:
    original = structured_png(1)
    h = phash_png_bytes(original)
    for variant in (variant_resized(original), variant_jpeg(original), variant_webp(original)):
        assert hamming(h, phash_png_bytes(variant)) <= 6
    unrelated = [phash_png_bytes(structured_png(seed)) for seed in range(100, 105)]
    for i, a in enumerate(unrelated):
        for b in unrelated[i + 1 :]:
            assert hamming(a, b) > 6


def test_video_frame_times() -> None:
    assert video_frame_times(10_000) == [(0, 1.0), (1, 3.0), (2, 5.0), (3, 7.0), (4, 9.0)]
    assert video_frame_times(1000)[0][0] == 0 and len(video_frame_times(1000)) == 5
    assert video_frame_times(600) == [(0, 0.3)]


async def test_video_frame_hashes(tmp_path: Path) -> None:
    video = make_lavfi_video(tmp_path / "v.mp4", seconds=4)
    hashes = await video_frame_hashes(video, 4000)
    assert sorted(hashes) == [0, 1, 2, 3, 4]
    assert len(set(hashes.values())) > 1  # testsrc2 moves, frames differ

    short = make_lavfi_video(tmp_path / "short.mp4", seconds=0.5)
    assert list(await video_frame_hashes(short, 500)) == [0]
