import random
import time

import numpy as np
import pytest

from app.dedup.hashing import to_signed
from app.dedup.index import BruteForceIndex, SimilarityIndex, popcount64


def reference_popcount(x: int) -> int:
    return bin(x).count("1")


def test_popcount_matches_reference() -> None:
    rng = random.Random(7)
    values = [rng.getrandbits(64) for _ in range(5000)]
    values += [v | (1 << 63) for v in values[:500]]
    values += [0, 1, 2**63, 2**64 - 1, 2**63 - 1]
    counts = popcount64(np.array(values, dtype=np.uint64))
    assert [int(c) for c in counts] == [reference_popcount(v) for v in values]


def test_query_against_brute_force_reference() -> None:
    rng = random.Random(11)
    base = rng.getrandbits(64)
    hashes = [base ^ (1 << rng.randrange(64)) ^ (1 << rng.randrange(64)) for _ in range(50)]
    hashes += [rng.getrandbits(64) | (1 << 63) for _ in range(200)]
    rows = [(i + 1, to_signed(h)) for i, h in enumerate(hashes)]
    index: SimilarityIndex = BruteForceIndex.from_signed(rows)
    assert len(index) == 250

    for threshold in (0, 2, 6, 20):
        expected = sorted(
            (
                (file_id, reference_popcount(h ^ base))
                for file_id, h in enumerate(hashes, start=1)
                if reference_popcount(h ^ base) <= threshold
            ),
            key=lambda r: (r[1], r[0]),
        )
        assert index.query(base, threshold) == expected


def test_query_excludes_the_query_file() -> None:
    index = BruteForceIndex.from_signed([(1, to_signed(2**64 - 1)), (2, to_signed(2**64 - 2))])
    assert index.query(2**64 - 1, 0) == [(1, 0)]
    assert index.query(2**64 - 1, 1, exclude_id=1) == [(2, 1)]
    assert BruteForceIndex.from_signed([]).query(0, 64) == []


@pytest.mark.slow
def test_query_over_100k_hashes_is_fast() -> None:
    rng = np.random.default_rng(5)
    ids = np.arange(1, 100_001, dtype=np.int64)
    hashes = rng.integers(0, 2**64, size=100_000, dtype=np.uint64, endpoint=False)
    index = BruteForceIndex(ids, hashes)
    target = int(hashes[123])
    index.query(target, 6)  # warm up
    started = time.perf_counter()
    found = index.query(target, 6, exclude_id=124)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"query took {elapsed:.3f}s"
    assert (124, 0) not in found
    assert all(d <= 6 for _, d in found)
