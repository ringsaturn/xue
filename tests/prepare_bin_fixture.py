from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from xue import binformat, zstdcli
from xue.binconvert import GridInfo, _variable_payloads, build_metadata, convert_bin
from xue.sources import source_spec

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_GRIB = REPOSITORY_ROOT / "tests" / "fixtures" / "gfs.2026081406.f000.crop.grib2"
GENERATED_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "generated"
WORK_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "work"

# A miniature GFS-shaped mixed-step axis: hourly frames, then a three-hourly
# tail — enough frames for full and partial temporal groups on both sides of
# the cadence change.
MIXED_HOURS = list(range(13)) + list(range(15, 37, 3))
MIXED_GRID = GridInfo(
    width=16,
    height=8,
    first_longitude=-180.0,
    first_latitude=90.0,
    longitude_step=22.5,
    latitude_step=-22.5,
)


def _mixed_plane(hour: int) -> np.ndarray:
    points = MIXED_GRID.width * MIXED_GRID.height
    return ((np.arange(points, dtype=np.uint16) * 3 + hour * 7) % 251).astype(np.uint8)


def prepare_mixed_axis_fixture() -> None:
    """Encode a synthetic mixed-axis bundle through the real encoder path
    (segment-aligned grouping, an explicitly listed ``hours`` axis) plus
    golden planes, so the Rust decoder's handling of both is held
    byte-identical too."""
    planes = {hour: _mixed_plane(hour) for hour in MIXED_HOURS}
    metadata = build_metadata(datetime(2026, 8, 14, 6, tzinfo=UTC), MIXED_HOURS, MIXED_GRID, "quality", ("tmp2m",))
    assert metadata["schemaVersion"] == 3
    assert "frameOffsets" in metadata["time"]
    payloads = []
    for entry, raw in _variable_payloads("tmp2m", MIXED_HOURS, planes):
        compressed = zstdcli.compress(raw)
        payloads.append(binformat.PlanePayload(replace(entry, compressed_length=len(compressed)), compressed))
    binformat.write_bundle(GENERATED_ROOT / "mixed.xue", metadata, payloads)
    bundle = binformat.read_bundle(GENERATED_ROOT / "mixed.xue")
    bundle.verify_all()
    for hour in MIXED_HOURS:
        plane = bundle.decode_plane(1, hour)
        (GENERATED_ROOT / f"expected.mixed.f{hour:03d}.bin").write_bytes(plane.tobytes())


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
    prepare_mixed_axis_fixture()
    return GENERATED_ROOT


if __name__ == "__main__":
    print(prepare_bin_fixture())
