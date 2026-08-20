"""eccodes CLI subprocess wrapper (same pattern as gdal.py / zstdcli.py).

Only needed on the ECMWF path: open data GRIB2 messages are CCSDS/AEC packed
(DRS template 5.42), which many GDAL builds cannot decode. ``grib_set``
repacks the downloaded messages to ``grid_simple`` so GDAL reads them
everywhere.
"""

from __future__ import annotations

from pathlib import Path

from .gdal import require_command, run_command


def repack_grid_simple(source: Path, destination: Path) -> None:
    run_command(
        [require_command("grib_set"), "-r", "-s", "packingType=grid_simple", str(source), str(destination)],
        description=f"repack {source} to grid_simple",
    )
