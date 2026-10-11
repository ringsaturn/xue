"""Cut the Himawari visible-band fixture: corners of real ISatSS tiles.

The infrared fixture tiles under ``tests/fixtures/himawari/`` are the
product's own files, untouched (a 2 km tile is 220 KB). The visible and
near-infrared bands the true colour composite reads come at 1 km (bands
1, 2 and 4: 1100 x 1100, 1.3 MB) and 0.5 km (band 3: 2200 x 2200,
4.7 MB), too big to commit sixteen of, so the fixture is the north-west
corner of each — a sixteenth of the tile's area, 275 x 275 cells at 1 km
and 550 x 550 at 0.5 km, the same ground either way — cut with
``gdal_translate -of netCDF -srcwin``, which keeps the ``geostationary``
grid mapping, the band's packing (reflectance at 1/2048, fill −32767,
unit ``1``) and the variable's name (checked here), deflated (the product
is not), so the reader opens a
cut tile exactly as it opens the product. The file names are the
product's, so the listing and the key parser see the real thing; the
corner is the sea south of Japan between 30°N and 32.5°N.

Usage: ``python tests/prepare_himawari_fixture.py [--source DIR]``.
Without a source directory the sixteen tiles are downloaded from the
public bucket (anonymous, ~35 MB in all); with one, they are read from
it. The tiles are T020 and T021 of the 03:00 and 03:10 UTC scans of
2026-09-17, the infrared fixture's own slots and tiles.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

BUCKET = "https://noaa-himawari9.s3.amazonaws.com"
#: The two scans' visible tiles, as the bucket names them (the ``c`` stamp
#: is the file's own and differs per channel).
KEYS = (
    "AHI-L2-FLDK-ISatSS/2026/09/17/0300/OR_HFD-010-B11-M1C01-T020_GH9_s20262600300000_c20262600308260.nc",
    "AHI-L2-FLDK-ISatSS/2026/09/17/0300/OR_HFD-010-B11-M1C01-T021_GH9_s20262600300000_c20262600308260.nc",
    "AHI-L2-FLDK-ISatSS/2026/09/17/0300/OR_HFD-010-B11-M1C02-T020_GH9_s20262600300000_c20262600308260.nc",
    "AHI-L2-FLDK-ISatSS/2026/09/17/0300/OR_HFD-010-B11-M1C02-T021_GH9_s20262600300000_c20262600308260.nc",
    "AHI-L2-FLDK-ISatSS/2026/09/17/0300/OR_HFD-005-B11-M1C03-T020_GH9_s20262600300000_c20262600308590.nc",
    "AHI-L2-FLDK-ISatSS/2026/09/17/0300/OR_HFD-005-B11-M1C03-T021_GH9_s20262600300000_c20262600308590.nc",
    "AHI-L2-FLDK-ISatSS/2026/09/17/0300/OR_HFD-010-B11-M1C04-T020_GH9_s20262600300000_c20262600308290.nc",
    "AHI-L2-FLDK-ISatSS/2026/09/17/0300/OR_HFD-010-B11-M1C04-T021_GH9_s20262600300000_c20262600308290.nc",
    "AHI-L2-FLDK-ISatSS/2026/09/17/0310/OR_HFD-010-B11-M1C01-T020_GH9_s20262600310000_c20262600318250.nc",
    "AHI-L2-FLDK-ISatSS/2026/09/17/0310/OR_HFD-010-B11-M1C01-T021_GH9_s20262600310000_c20262600318250.nc",
    "AHI-L2-FLDK-ISatSS/2026/09/17/0310/OR_HFD-010-B11-M1C02-T020_GH9_s20262600310000_c20262600318280.nc",
    "AHI-L2-FLDK-ISatSS/2026/09/17/0310/OR_HFD-010-B11-M1C02-T021_GH9_s20262600310000_c20262600318280.nc",
    "AHI-L2-FLDK-ISatSS/2026/09/17/0310/OR_HFD-005-B11-M1C03-T020_GH9_s20262600310000_c20262600319080.nc",
    "AHI-L2-FLDK-ISatSS/2026/09/17/0310/OR_HFD-005-B11-M1C03-T021_GH9_s20262600310000_c20262600319080.nc",
    "AHI-L2-FLDK-ISatSS/2026/09/17/0310/OR_HFD-010-B11-M1C04-T020_GH9_s20262600310000_c20262600318280.nc",
    "AHI-L2-FLDK-ISatSS/2026/09/17/0310/OR_HFD-010-B11-M1C04-T021_GH9_s20262600310000_c20262600318280.nc",
)
#: The corner kept, as a fraction of the tile's side: a quarter of each
#: side, a sixteenth of the area.
CORNER = 4
VARIABLE = "Sectorized_CMI"
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "himawari"


def cut(source: Path, out: Path) -> None:
    info = json.loads(subprocess.run(["gdalinfo", "-json", f'NETCDF:"{source}":{VARIABLE}'], check=True, capture_output=True, text=True).stdout)
    width, height = info["size"]
    subprocess.run(
        [
            "gdal_translate",
            "-q",
            "-of",
            "netCDF",
            "-co",
            "FORMAT=NC4",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "ZLEVEL=9",
            "-srcwin",
            "0",
            "0",
            str(width // CORNER),
            str(height // CORNER),
            f'NETCDF:"{source}":{VARIABLE}',
            str(out),
        ],
        check=True,
    )
    info = json.loads(subprocess.run(["gdalinfo", "-json", f'NETCDF:"{out}":{VARIABLE}'], check=True, capture_output=True, text=True).stdout)
    wkt = info["coordinateSystem"]["wkt"]
    band = info["bands"][0]
    if "Geostationary" not in wkt:
        raise SystemExit(f"{out}: the cut lost the geostationary projection")
    if band.get("noDataValue") != -32767 or band.get("scale") != 0.00048828125 or band.get("offset") != 0 or band.get("unit") != "1":
        raise SystemExit(f"{out}: the cut lost the band's packing: {band}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", type=Path, help="a directory already holding the sixteen tiles")
    args = parser.parse_args(argv)
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for key in KEYS:
        name = key.rsplit("/", 1)[-1]
        source = (args.source or FIXTURE_DIR.parent / "himawari-raw") / name
        if not source.is_file():
            if args.source is not None:
                raise SystemExit(f"{source} is missing")
            source.parent.mkdir(parents=True, exist_ok=True)
            print(f"downloading {name}", file=sys.stderr)
            urllib.request.urlretrieve(f"{BUCKET}/{key}", source)
        out = FIXTURE_DIR / name
        cut(source, out)
        print(f"{out.name}: {out.stat().st_size} bytes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
