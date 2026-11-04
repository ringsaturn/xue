"""The first-frame poster (mirrored in ``encode/poster.rs``): one quantized
plane, decimated and row-delta filtered, that a variable switch paints
while the real stream loads.
"""

from __future__ import annotations

import zlib

import numpy as np

from .grid import GridInfo


def encode_poster(codes: np.ndarray, grid: GridInfo) -> tuple[bytes, GridInfo]:
    """Encode one quantized plane as a small first-frame poster.

    The plane is decimated 2x in both axes (row/column 0, 2, 4, ... — for the
    721-row grid the last row still lands exactly on the south pole), rows are
    delta-filtered against the previous row (PNG "Up", uint8 wraparound), and
    the result is zlib-deflated so the browser can inflate it with the native
    ``DecompressionStream("deflate")`` — no WASM on the poster path.
    """
    plane = codes.reshape(grid.height, grid.width)[::2, ::2]
    poster_grid = grid.decimated()
    filtered = plane.copy()
    filtered[1:] -= plane[:-1]
    return zlib.compress(filtered.tobytes(), 9), poster_grid


def decode_poster(payload: bytes, width: int, height: int) -> np.ndarray:
    """Reference decoder for :func:`encode_poster`, used by tests."""
    filtered = np.frombuffer(zlib.decompress(payload), dtype=np.uint8).reshape(height, width).copy()
    return np.cumsum(filtered, axis=0, dtype=np.uint8)
