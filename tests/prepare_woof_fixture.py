"""Cut ``tests/fixtures/woof.2026101006/`` from the Fuji WOOF run.

Runs ``xue wrf-series`` on the run directory (``~/Downloads/run_20294738511688ee``
or the one argument) into a scratch directory, then keeps forecast hours 1
and 2 and a 20 by 16 cell window of the 79 by 61 grid around the Fuji
summit cell (138.7276°E, 35.3642°N), every attribute intact. Run from the
repository root:

    .venv/bin/python tests/prepare_woof_fixture.py [RUN_DIR]
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import netCDF4
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xuebuild.wrf import VARIABLE_IDS, convert_run  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "woof.2026101006"
HOURS = slice(0, 2)
#: Columns 28..47 (138.680°E to 138.775°E) and rows 21..36 (35.325°N to
#: 35.400°N) of the 0.005° grid.
COLUMNS = slice(28, 48)
ROWS = slice(21, 37)


def cut(source: Path, target: Path) -> None:
    with netCDF4.Dataset(source) as src, netCDF4.Dataset(target, "w", format="NETCDF4") as dst:
        attributes = {name: src.getncattr(name) for name in src.ncattrs()}
        attributes["history"] += f"; tests/prepare_woof_fixture.py: hours 1-2, columns {COLUMNS.start}-{COLUMNS.stop - 1}, rows {ROWS.start}-{ROWS.stop - 1}"
        dst.setncatts(attributes)
        windows = {"time": HOURS, "latitude": ROWS, "longitude": COLUMNS}
        for name, dimension in src.dimensions.items():
            size = len(range(*windows[name].indices(len(dimension)))) if name in windows else len(dimension)
            dst.createDimension(name, None if dimension.isunlimited() else size)
        for name, variable in src.variables.items():
            fill = variable.getncattr("_FillValue") if "_FillValue" in variable.ncattrs() else None
            copy = dst.createVariable(name, variable.dtype, variable.dimensions, zlib=bool(variable.filters()["zlib"]), complevel=1, fill_value=fill)
            copy.setncatts({key: variable.getncattr(key) for key in variable.ncattrs() if key != "_FillValue"})
            index = tuple(windows.get(dimension, slice(None)) for dimension in variable.dimensions)
            copy[...] = variable[index] if variable.dimensions else variable[...]


def main(argv: list[str]) -> int:
    run_dir = Path(argv[1]).expanduser() if len(argv) > 1 else Path.home() / "Downloads" / "run_20294738511688ee"
    scratch = Path(tempfile.mkdtemp(prefix="xue-woof-"))
    try:
        command = f"xue wrf-series {run_dir} --domain d04 --out <scratch>"
        for summary in convert_run(run_dir, "d04", scratch, command=command):
            print(summary.line())
        FIXTURE.mkdir(parents=True, exist_ok=True)
        for variable_id in VARIABLE_IDS:
            name = f"woof.2026101006.{variable_id}.nc"
            cut(scratch / name, FIXTURE / name)
        with netCDF4.Dataset(FIXTURE / "woof.2026101006.orog.nc") as orog:
            heights = np.asarray(orog["orog"][0])
            row, column = np.unravel_index(int(heights.argmax()), heights.shape)
            print(f"summit cell in the fixture: {heights.max():.0f} m at {orog['longitude'][column]:.3f}E {orog['latitude'][row]:.3f}N")
        total = sum(path.stat().st_size for path in FIXTURE.iterdir())
        print(f"{FIXTURE}: {len(list(FIXTURE.iterdir()))} files, {total / 1024:.0f} KB")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
