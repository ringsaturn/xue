from __future__ import annotations

from pathlib import Path

from xue import binformat
from xue.binconvert import convert_bin
from xue.sources import source_spec

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_GRIB = REPOSITORY_ROOT / "tests" / "fixtures" / "gfs.2026081406.f000.crop.grib2"
GENERATED_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "generated"
WORK_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "work"


def prepare_bin_fixture() -> Path:
    """Encode the cropped GRIB fixture and dump Python-decoded golden planes."""
    GENERATED_ROOT.mkdir(parents=True, exist_ok=True)
    # The cropped fixture GRIB predates the wind components, so this
    # builds the scalar bundles only; the golden planes cover those.
    convert_bin(FIXTURE_GRIB, GENERATED_ROOT, work_root=WORK_ROOT)
    for name in source_spec("gfs").bundle_scalar_ids:
        bundle = binformat.read_bundle(GENERATED_ROOT / f"{name}.xue")
        for numeric_id in sorted(bundle.variable_ids):
            for hour in bundle.forecast_hours:
                plane = bundle.decode_plane(numeric_id, hour)
                expected = GENERATED_ROOT / f"expected.{name}.f{hour:03d}.bin"
                expected.write_bytes(plane.tobytes())
    return GENERATED_ROOT


if __name__ == "__main__":
    print(prepare_bin_fixture())
