"""Hamming-distance search over 64-bit hashes.

SimilarityIndex is the interface the rest of the app uses; BruteForceIndex scans a numpy uint64
array and can be swapped for a BK-tree later without touching callers.
"""

from collections.abc import Iterable
from typing import Protocol

import numpy as np
import numpy.typing as npt


class SimilarityIndex(Protocol):
    def __len__(self) -> int: ...

    def query(
        self, value: int, threshold: int, exclude_id: int | None = None
    ) -> list[tuple[int, int]]:
        """(file_id, distance) for every hash within threshold bits of value, nearest first.

        value is an unsigned 64-bit hash; exclude_id (the query file) is left out.
        """
        ...


def popcount64(values: npt.NDArray[np.uint64]) -> npt.NDArray[np.uint8]:
    """Number of set bits of each uint64."""
    return np.bitwise_count(values)


class BruteForceIndex:
    def __init__(self, ids: npt.NDArray[np.int64], hashes: npt.NDArray[np.uint64]) -> None:
        if ids.shape != hashes.shape:
            raise ValueError("ids and hashes must have the same shape")
        self.ids = ids
        self.hashes = hashes

    @classmethod
    def from_signed(cls, rows: Iterable[tuple[int, int]]) -> "BruteForceIndex":
        """Build from (file_id, signed int64 hash) rows as stored in the database."""
        pairs = list(rows)
        ids = np.fromiter((r[0] for r in pairs), dtype=np.int64, count=len(pairs))
        signed = np.fromiter((r[1] for r in pairs), dtype=np.int64, count=len(pairs))
        return cls(ids, signed.view(np.uint64))

    def __len__(self) -> int:
        return int(self.ids.shape[0])

    def query(
        self, value: int, threshold: int, exclude_id: int | None = None
    ) -> list[tuple[int, int]]:
        if len(self) == 0:
            return []
        distances = popcount64(np.bitwise_xor(self.hashes, np.uint64(value)))
        mask = distances <= threshold
        if exclude_id is not None:
            mask &= self.ids != exclude_id
        found = np.flatnonzero(mask)
        order = np.lexsort((self.ids[found], distances[found]))
        found = found[order]
        return [(int(i), int(d)) for i, d in zip(self.ids[found], distances[found], strict=True)]
