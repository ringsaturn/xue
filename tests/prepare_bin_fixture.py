from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from xuebuild import binformat, temporal, zstdcli
from xuebuild.binconvert import GridInfo, build_metadata, convert_bin
from xuebuild.sources import source_spec

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_GRIB = REPOSITORY_ROOT / "tests" / "fixtures" / "gfs.2026081406.f000.crop.grib2"
GENERATED_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "generated"
WORK_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "work"

# A miniature GFS-shaped mixed-step axis: hourly frames, then a three-hourly
# tail — enough frames for full and partial temporal groups on both sides of
# the cadence change, and a trailing short group.
MIXED_HOURS = list(range(13)) + list(range(15, 37, 3))


# A grid no tile size divides: 17 x 9 cells cut into 5 x 4 tiles leaves a
# two-column last tile column and a one-row last tile row, so the clipped
# tiles the production 721-row grid always has are exercised on every side.
TILED_GRID = GridInfo(
    width=17,
    height=9,
    first_longitude=-180.0,
    first_latitude=90.0,
    longitude_step=21.0,
    latitude_step=-20.0,
)
TILE_SIZE = (5, 4)
# tmp2m (numericId 1) is a linear field and chains against the previous frame;
# prate (2) is stacked RAW, the way precipitation must be. Two variables also
# put the (tile, variable) interleave of a wind bundle under test.
TILED_PREDICTORS = {1: binformat.PREDICTOR_PREVIOUS, 2: binformat.PREDICTOR_RAW}


def _tiled_plane(hour: int, variable_id: int) -> np.ndarray:
    points = TILED_GRID.width * TILED_GRID.height
    return ((np.arange(points, dtype=np.uint32) * (3 + variable_id) + hour * 7) % 251).astype(np.uint8)


def prepare_tiled_fixture() -> None:
    """Encode a synthetic container v2 bundle and dump golden planes and a
    golden cell series, so the Rust decoder is held to the Python reference on
    the tiled layout too — clipped tiles, both predictors, and the mixed-step
    axis whose last group is short."""
    planes = {
        hour: {variable_id: _tiled_plane(hour, variable_id) for variable_id in TILED_PREDICTORS}
        for hour in MIXED_HOURS
    }
    metadata = build_metadata(
        datetime(2026, 8, 14, 6, tzinfo=UTC), MIXED_HOURS, TILED_GRID, "quality", ("tmp2m", "prate")
    )
    tiles = binformat.TileGeometry(TILED_GRID.width, TILED_GRID.height, *TILE_SIZE)
    variables, groups, raw_chunks = temporal.build_chunks(MIXED_HOURS, planes, tiles, TILED_PREDICTORS)
    chunks = []
    for entry, stored in raw_chunks:
        payload = zstdcli.compress(stored.tobytes())
        chunks.append(
            binformat.ChunkPayload(replace(entry, compressed_length=len(payload)), payload)
        )
    binformat.write_bundle_v2(
        GENERATED_ROOT / "tiled.xue",
        metadata,
        tile_width=TILE_SIZE[0],
        tile_height=TILE_SIZE[1],
        variables=variables,
        groups=groups,
        chunks=chunks,
    )
    bundle = binformat.read_bundle(GENERATED_ROOT / "tiled.xue")
    bundle.verify_all()
    for variable_id in TILED_PREDICTORS:
        for hour in MIXED_HOURS:
            plane = bundle.decode_plane(variable_id, hour)
            (GENERATED_ROOT / f"expected.tiled.v{variable_id}.f{hour:03d}.bin").write_bytes(plane.tobytes())
        # The south-east corner cell lives in the doubly clipped last tile.
        series = bundle.decode_series(variable_id, TILED_GRID.width - 1, TILED_GRID.height - 1)
        (GENERATED_ROOT / f"expected.tiled.v{variable_id}.series.bin").write_bytes(series.tobytes())


def prepare_bin_fixture() -> Path:
    """Encode the cropped GRIB fixture and dump Python-decoded golden planes."""
    GENERATED_ROOT.mkdir(parents=True, exist_ok=True)
    # The cropped fixture GRIB predates the wind components, so this
    # builds the scalar bundles only; the golden planes cover those.
    convert_bin(FIXTURE_GRIB, GENERATED_ROOT, work_root=WORK_ROOT)
    for name in source_spec("gfs").bundle_scalar_ids:
        bundle = binformat.read_bundle(GENERATED_ROOT / f"{name}.xue")
        for numeric_id in sorted(bundle.variable_ids):
            for hour in bundle.frame_offsets:
                plane = bundle.decode_plane(numeric_id, hour)
                expected = GENERATED_ROOT / f"expected.{name}.f{hour:03d}.bin"
                expected.write_bytes(plane.tobytes())
    prepare_tiled_fixture()
    return GENERATED_ROOT


if __name__ == "__main__":
    print(prepare_bin_fixture())
