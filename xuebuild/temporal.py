"""Xue temporal grouping and residual coding.

Residuals are one-byte modulo-256 wrapping differences. Wrapping subtraction
and addition are lossless for every byte pair, so there is no residual range
check, no signed interpretation, and no RAW fallback.

Grouping is shared by both container versions: a group is a run of
consecutive frames formed inside a segment of constant step. What differs is
what a group *is* physically — v1 stores one plane per frame with a middle
anchor, v2 stores one chunk per tile holding the group's frames back to back,
predicted against the previous frame of the same chunk.
"""

from __future__ import annotations

import numpy as np

from .binformat import (
    PREDICTOR_PREVIOUS,
    PREDICTOR_RAW,
    ChunkEntry,
    GroupEntry,
    TileGeometry,
    VariableEntry,
    crc32_plane,
)
from .errors import ConversionError

GROUP_LENGTH = 6


def encode_residual(current: np.ndarray, base: np.ndarray) -> np.ndarray:
    if current.shape != base.shape:
        raise ConversionError("plane lengths differ")
    return (current.astype(np.uint8) - base.astype(np.uint8)).astype(np.uint8)


def decode_residual(residual: np.ndarray, base: np.ndarray) -> np.ndarray:
    if residual.shape != base.shape:
        raise ConversionError("plane lengths differ")
    return (residual.astype(np.uint8) + base.astype(np.uint8)).astype(np.uint8)


def split_segments(hours: list[int]) -> list[list[int]]:
    """Maximal runs of constant step (docs/format.md "segment"): a boundary
    falls between two frames exactly where the step changes, so a uniform
    axis is one segment and the GFS 240-hour axis is two."""
    if hours != sorted(hours) or len(set(hours)) != len(hours):
        raise ConversionError("forecast hours must be unique and ascending")
    segments = [[hours[0]]]
    step: int | None = None
    for previous, current in zip(hours, hours[1:]):
        if step is not None and current - previous != step:
            segments.append([current])
        else:
            segments[-1].append(current)
        step = current - previous
    return segments


def group_forecast_hours(hours: list[int], group_length: int = GROUP_LENGTH) -> list[list[int]]:
    """Temporal groups, formed inside segments of constant step so no group
    straddles a change of cadence (an ANCHOR residual is then always a
    difference between frames one step apart)."""
    return [
        segment[start : start + group_length]
        for segment in split_segments(hours)
        for start in range(0, len(segment), group_length)
    ]


def anchor_hour(group: list[int]) -> int:
    """The anchor is the frame at zero-based index ``floor(n / 2)`` in its group."""
    if not group:
        raise ConversionError("temporal group is empty")
    return group[len(group) // 2]


def tile_stack(planes: list[np.ndarray], tiles: TileGeometry, tile: int) -> np.ndarray:
    """One tile's block of a group's planes, as ``(frames, height, width)``.

    ``planes`` are the group's frames in axis order, each a flat plane of the
    grid ``tiles`` describes. The block is the tile's *clipped* rectangle, so
    the last column and the last row come out narrower or shorter.
    """
    row, column = tiles.origin(tile)
    height, width = tiles.shape(tile)
    return np.stack(
        [
            plane.reshape(tiles.height, tiles.width)[row : row + height, column : column + width]
            for plane in planes
        ]
    )


def encode_chunk(block: np.ndarray, predictor: int) -> np.ndarray:
    """The stored bytes of one chunk, frame-major.

    RAW stacks the codes outright — the only encoding precipitation tolerates.
    PREVIOUS keeps the first frame whole and stores every later frame as its
    modulo-256 difference against the frame before it in the same chunk, which
    is the preceding frame on the axis because a group is contiguous.
    """
    if predictor == PREDICTOR_RAW:
        return block
    if predictor != PREDICTOR_PREVIOUS:
        raise ConversionError(f"predictor {predictor} is not valid in a v2 chunk")
    stored = block.copy()
    stored[1:] = (block[1:] - block[:-1]).astype(np.uint8)
    return stored


def build_chunks(
    offsets: list[int],
    planes: dict[int, dict[int, np.ndarray]],
    tiles: TileGeometry,
    predictors: dict[int, int],
    group_length: int = GROUP_LENGTH,
) -> tuple[list[VariableEntry], list[GroupEntry], list[tuple[ChunkEntry, np.ndarray]]]:
    """The v2 index tables and the uncompressed chunks of one bundle.

    ``planes`` is ``frame offset -> numeric variable id -> flat plane``, and
    every variable shares the file's one axis and therefore its groups. The
    chunks come back in the physical order the spec fixes — group, then tile
    row-major, then variable by ascending id — which is what makes a whole
    group one contiguous range, a viewport's tile row another, and a cell's
    series one chunk per group. The caller compresses each chunk and fills in
    its ``compressedLength``; the CRC32 here already covers the reconstruction.
    """
    variable_ids = sorted(predictors)
    variables = [
        VariableEntry(variable_id=variable_id, predictor=predictors[variable_id])
        for variable_id in variable_ids
    ]
    groups: list[GroupEntry] = []
    chunks: list[tuple[ChunkEntry, np.ndarray]] = []
    first_frame = 0
    for group in group_forecast_hours(offsets, group_length):
        groups.append(GroupEntry(first_frame=first_frame, frame_count=len(group)))
        first_frame += len(group)
        for tile in range(tiles.count):
            for variable_id in variable_ids:
                block = tile_stack([planes[offset][variable_id] for offset in group], tiles, tile)
                stored = encode_chunk(block, predictors[variable_id])
                chunks.append((ChunkEntry(compressed_length=0, crc32=crc32_plane(block)), stored))
    return variables, groups, chunks
