#!/usr/bin/env python3
"""Measure the per-frame raster-tile baseline from the README size table.

Converts 2 m temperature and precipitation rate from one GFS run into
per-frame precolored PNG8 PMTiles (EPSG:3857, zoom 0-4) and reports the
byte totals. Each frame is built in a temporary directory, measured, and
deleted - the printed numbers are the only output.

Requires the GDAL CLI tools and the pmtiles CLI, plus the source run:

    python -m xue fetch --run 2026081506
    python scripts/pmtiles_size_assessment/measure.py        # 6-hour sample
    python scripts/pmtiles_size_assessment/measure.py --all  # exact total
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE.parents[1] / "data/raw/gfs.2026081506"
VARIABLES = {
    "tmp2m": ("TMP", "maximum(-60,minimum(50,A))", HERE / "temperature-palette.txt"),
    "prate": ("PRATE", "maximum(0,minimum(50,A*3600))", HERE / "precipitation-palette.txt"),
}
DEFAULT_HOURS = (0, 24, 48, 72, 96, 120)
FULL_RUN_FRAMES = 121 * len(VARIABLES)
MERCATOR = "20037508.342789244"


def run(arguments: list) -> None:
    subprocess.run([str(a) for a in arguments], check=True, capture_output=True)


def grib_band(source: Path, element: str) -> int:
    info = json.loads(
        subprocess.run(
            ["gdalinfo", "-json", str(source)], check=True, capture_output=True, text=True
        ).stdout
    )
    bands = [
        int(band["band"])
        for band in info["bands"]
        if band.get("metadata", {}).get("", {}).get("GRIB_ELEMENT", "").upper() == element
    ]
    if len(bands) != 1:
        raise RuntimeError(f"expected one {element} band in {source.name}, found {bands}")
    return bands[0]


def pmtiles_bytes(variable: str, hour: int) -> int:
    element, expression, palette = VARIABLES[variable]
    source = RAW / f"gfs.2026081506.f{hour:03d}.grib2"
    with tempfile.TemporaryDirectory(prefix=f"{variable}-f{hour:03d}-") as temporary:
        work = Path(temporary)
        run(["gdal_translate", "-q", "-b", grib_band(source, element), "-of", "GTiff",
             source, work / "selected.tif"])
        run(["gdal_calc.py", "-A", work / "selected.tif",
             f"--outfile={work / 'physical.tif'}", f"--calc={expression}",
             "--NoDataValue=-9999", "--type=Float32", "--quiet"])
        run(["gdalwarp", "-q", "-t_srs", "EPSG:3857",
             "-te", f"-{MERCATOR}", f"-{MERCATOR}", MERCATOR, MERCATOR,
             "-ts", "4096", "4096", "-r", "bilinear",
             "-srcnodata", "-9999", "-dstnodata", "-9999",
             work / "physical.tif", work / "projected.tif"])
        run(["gdaldem", "color-relief", "-q", "-alpha",
             work / "projected.tif", palette, work / "colored.tif"])
        run(["gdal_translate", "-q", "-of", "MBTILES", "-co", "TILE_FORMAT=PNG8",
             "-co", "ZOOM_LEVEL_STRATEGY=LOWER", work / "colored.tif", work / "tiles.mbtiles"])
        run(["gdaladdo", "-q", "-r", "average", work / "tiles.mbtiles", "2", "4", "8", "16"])
        run(["pmtiles", "convert", work / "tiles.mbtiles", work / "frame.pmtiles"])
        return (work / "frame.pmtiles").stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="measure all 121 forecast hours")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    hours = tuple(range(121)) if args.all else DEFAULT_HOURS

    missing = [f"f{hour:03d}" for hour in hours
               if not (RAW / f"gfs.2026081506.f{hour:03d}.grib2").is_file()]
    if missing:
        raise SystemExit(
            f"missing GRIB2 files under {RAW}: {', '.join(missing)}\n"
            "fetch them first: python -m xue fetch --run 2026081506"
        )
    source_bytes = sum(path.stat().st_size for path in RAW.glob("*.grib2"))

    tasks = [(variable, hour) for variable in VARIABLES for hour in hours]
    total = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {executor.submit(pmtiles_bytes, *task): task for task in tasks}
        for position, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            variable, hour = futures[future]
            size = future.result()
            total += size
            print(f"[{position}/{len(tasks)}] {variable} f{hour:03d} {size / 1e6:.3f} MB",
                  flush=True)

    print(f"\nsource GRIB2: {source_bytes:,} bytes = {source_bytes / 1e6:.2f} MB")
    print(f"PMTiles, {len(tasks)} frames measured: {total:,} bytes = {total / 1e6:.2f} MB")
    if len(tasks) < FULL_RUN_FRAMES:
        projected = round(total / len(tasks) * FULL_RUN_FRAMES)
        print(f"extrapolated to {FULL_RUN_FRAMES} frames: {projected:,} bytes"
              f" = {projected / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
