"""The resolution ladder (mirrored in ``encode/ladder.rs``): the reduced
tiers a source publishes beside its full bundles, each decimated from the
already-quantized full codes, with the grid, the tile and the playback
bandwidth hint every rung is written with.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import numpy as np

from .grid import GridInfo
from .quantize import PROFILES
from .variables import variable_spec


# The resolution ladder's tier names by decimation factor: the suffix of
# the rendition's files (`<bundle>.half.xue`, the shared `bundles.half.zarr`) and its
# STAC `xue:tier`. A source's `SourceSpec.variant_factors` picks its rungs
# (the half alone for most grids, the half and the quarter on the
# satellite disks and the 0.1° IFS grids); the eighth is named because a
# run published with it stays readable. Any other factor is a registry bug,
# not a runtime case.
VARIANT_TIERS: dict[int, str] = {2: "half", 4: "quarter", 8: "eighth"}


def variant_tier(factor: int) -> str:
    """The tier name of the rendition decimated by ``factor``: ``half``,
    ``quarter`` or ``eighth`` (:data:`VARIANT_TIERS`). A factor the ladder
    has no name for is a registry bug, and raised as one."""
    try:
        return VARIANT_TIERS[factor]
    except KeyError:
        raise ValueError(f"no resolution tier is decimated by {factor}") from None


def variant_grid(grid: GridInfo, factor: int) -> GridInfo:
    """The grid of the rendition decimated by ``factor``: :meth:`GridInfo.decimated`
    applied log2(factor) times, so 3000 rows go 1500, 750, 375 and the
    721-row production grid 361, 181, 91 (a ceiling at every halving)."""
    for _ in range(factor.bit_length() - 1):
        grid = grid.decimated()
    return grid


def bundle_tile(tile: tuple[int, int], grid: GridInfo, *, factor: int) -> tuple[int, int]:
    """The tile size one bundle is cut with: the source tile divided by the
    rendition's decimation ``factor`` (1 for the full tier), rounded up.

    A reduced-resolution variant divides the source tile by its factor, so
    tile number n covers the same ground in every tier and a viewport keeps
    its tile rectangle across a tier switch; the ceiling at each rung is the
    ceiling of the rung before halved (``ceil(ceil(n / 2) / 2) == ceil(n /
    4)``), so the quarter's tile is the half's halved. Either size is then
    clamped to the grid, because the format requires ``1 <= tile <= grid``
    so that a single-tile file states its grid size exactly — and a regional
    crop is routinely smaller than the source's tile (a six-degree showcase
    window is 24 x 24 cells against the 0.25-degree grid's 48 x 52 tile).
    Clamping makes such a file one tile, which is the right answer: there is
    nothing left to subdivide.
    """
    tile = ((tile[0] + factor - 1) // factor, (tile[1] + factor - 1) // factor)
    return min(tile[0], grid.width), min(tile[1], grid.height)


def decimate_codes(codes: np.ndarray, grid: GridInfo, factor: int = 2) -> np.ndarray:
    """Reduced-resolution copy of one quantized plane: every ``factor``-th
    row and column from the origin (rows/columns 0, 2, 4, ... at the half),
    matching the poster decimation so every tier shares the same sample
    sites. Decimating by 4 is decimating by 2 twice, so a rung is always
    taken from the full plane and never from the rung before."""
    return np.ascontiguousarray(codes.reshape(grid.height, grid.width)[::factor, ::factor]).ravel()


def decimate_peak_codes(codes: np.ndarray, grid: GridInfo, factor: int, nodata_code: int) -> np.ndarray:
    """Reduced-resolution copy of one quantized plane that keeps peaks: at
    each site :func:`decimate_codes` keeps (rows and columns 0, ``factor``,
    2 ``factor``, ...), the largest code in the window of ``factor // 2``
    rows and columns either side of it, clipped at the plane's edges. The
    windows overlap by a row and a column, so every cell of the full plane
    reaches a site, and a peak moves at most half a reduced cell. A
    linear codebook's codes rise with the value; ``nodata_code`` counts
    below every code and is the site's only when its whole window is
    nodata."""
    half = factor // 2
    plane = codes.reshape(grid.height, grid.width).astype(np.int16)
    plane[plane == nodata_code] = -1
    padded = np.pad(plane, half, constant_values=-1)
    peak = np.full(((grid.height + factor - 1) // factor, (grid.width + factor - 1) // factor), -1, dtype=np.int16)
    for row in range(2 * half + 1):
        for column in range(2 * half + 1):
            np.maximum(peak, padded[row : row + grid.height : factor, column : column + grid.width : factor], out=peak)
    peak[peak < 0] = nodata_code
    return np.ascontiguousarray(peak.astype(np.uint8)).ravel()


def decimate_variant_codes(variable_id: str, codes: np.ndarray, grid: GridInfo, factor: int, profile: str) -> np.ndarray:
    """One plane's reduced tier by the variable's ``decimation`` rule: the
    peak-keeping decimation (:func:`decimate_peak_codes`) for a variable
    whose full tier is already the peak of the source pixels and whose
    one-cell hot spots must survive the half tier — the 3.9 µm window,
    published to show fires — or the site's own code."""
    if variable_spec(variable_id).decimation == "peak":
        return decimate_peak_codes(codes, grid, factor, PROFILES[profile][variable_id].nodata_code)
    return decimate_codes(codes, grid, factor)


def playback_bandwidth(byte_length: int, frame_count: int, *, fps: float = 12.0) -> int:
    """HLS STREAM-INF style bandwidth hint: average bits per second needed to
    keep up with the 12 fps playback rate while downloading the whole tier."""
    return max(1, round(byte_length * 8 * fps / max(1, frame_count)))


def build_ladder(
    variant_factors: tuple[int, ...],
    grids: Mapping[str | None, GridInfo],
    codes_by_offset: Mapping[int, Mapping[str, np.ndarray]],
    offsets: Sequence[int],
    grid_for: Callable[[str], GridInfo],
    profile: str,
) -> tuple[dict[int, dict[str | None, GridInfo]], dict[int, dict[int, dict[str, np.ndarray]]]]:
    """Every rung of the ladder: its grid per family, and its codes per
    frame offset, decimated from the full codes by each variable's rule."""
    # One rung per factor per family: a family's rung is its own grid
    # decimated, so a bundle's tier keeps the tile geometry of its own grid.
    variant_grids = {
        factor: {family: variant_grid(family_grid, factor) for family, family_grid in grids.items()}
        for factor in variant_factors
    }
    # Iterate the codes actually present per hour: derived prate has no
    # analysis-frame plane on ECMWF/sflux.
    variant_codes_by_offset = {
        factor: {
            offset: {
                variable_id: decimate_variant_codes(variable_id, codes, grid_for(variable_id), factor, profile)
                for variable_id, codes in codes_by_offset[offset].items()
            }
            for offset in offsets
        }
        for factor in variant_factors
    }
    return variant_grids, variant_codes_by_offset
