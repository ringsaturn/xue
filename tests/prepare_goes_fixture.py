"""Cut the GOES CMIPF fixture: eighteen small windows of real full-disk files.

A CMIPF file is one 24 MB netCDF per channel per scan (a 2 km infrared
channel; the 1 km visible channels are 100 MB and the 0.5 km red one 430
MB), too big to commit, so the fixture is a window of the ``CMI`` variable
of each — the six channels the Dust RGB and DEBRA read (C07, C08, C11,
C13, C14, C15) and the three the true colour composite reads (C01, C02,
C03) of two consecutive GOES-19 scans — cut with ``gdal_translate -of
netCDF -srcwin``, which keeps the ``geostationary`` grid mapping, the
band's scale, offset, unit and fill (checked here), so the reader opens a
cut file exactly as it opens the product. The window is the disk's 2 km
pixels 3200–3419 x 2100–2319, the Venezuelan coast and Trinidad (66–62°W,
7–11°N), land, sea and cloud; a finer channel's window is the same ground
in its own pixels (twice or four times the offset and side), deflated,
since the product is not.

Usage: ``python tests/prepare_goes_fixture.py [--source DIR]``. Without a
source directory the eighteen files are downloaded from the public bucket
(anonymous, ~1.5 GB in all); with one, they are read from it. The cut
files keep the product's own names under ``tests/fixtures/goes/``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

BUCKET = "https://noaa-goes19.s3.amazonaws.com"
#: The two scans (2026-09-17 15:10 and 15:20 UTC, day 260) and their six
#: dust channels and three solar bands, as the bucket names them.
KEYS = (
    "ABI-L2-CMIPF/2026/260/15/OR_ABI-L2-CMIPF-M6C01_G19_s20262601510206_e20262601519514_c20262601519573.nc",
    "ABI-L2-CMIPF/2026/260/15/OR_ABI-L2-CMIPF-M6C02_G19_s20262601510206_e20262601519514_c20262601519566.nc",
    "ABI-L2-CMIPF/2026/260/15/OR_ABI-L2-CMIPF-M6C03_G19_s20262601510206_e20262601519514_c20262601519573.nc",
    "ABI-L2-CMIPF/2026/260/15/OR_ABI-L2-CMIPF-M6C07_G19_s20262601510206_e20262601519526_c20262601519576.nc",
    "ABI-L2-CMIPF/2026/260/15/OR_ABI-L2-CMIPF-M6C08_G19_s20262601510206_e20262601519514_c20262601519586.nc",
    "ABI-L2-CMIPF/2026/260/15/OR_ABI-L2-CMIPF-M6C11_G19_s20262601510206_e20262601519514_c20262601519583.nc",
    "ABI-L2-CMIPF/2026/260/15/OR_ABI-L2-CMIPF-M6C13_G19_s20262601510206_e20262601519526_c20262601519573.nc",
    "ABI-L2-CMIPF/2026/260/15/OR_ABI-L2-CMIPF-M6C14_G19_s20262601510206_e20262601519514_c20262601519591.nc",
    "ABI-L2-CMIPF/2026/260/15/OR_ABI-L2-CMIPF-M6C15_G19_s20262601510206_e20262601519520_c20262601519581.nc",
    "ABI-L2-CMIPF/2026/260/15/OR_ABI-L2-CMIPF-M6C01_G19_s20262601520206_e20262601529514_c20262601529570.nc",
    "ABI-L2-CMIPF/2026/260/15/OR_ABI-L2-CMIPF-M6C02_G19_s20262601520206_e20262601529514_c20262601529563.nc",
    "ABI-L2-CMIPF/2026/260/15/OR_ABI-L2-CMIPF-M6C03_G19_s20262601520206_e20262601529514_c20262601529581.nc",
    "ABI-L2-CMIPF/2026/260/15/OR_ABI-L2-CMIPF-M6C07_G19_s20262601520206_e20262601529525_c20262601529593.nc",
    "ABI-L2-CMIPF/2026/260/15/OR_ABI-L2-CMIPF-M6C08_G19_s20262601520206_e20262601529514_c20262601529593.nc",
    "ABI-L2-CMIPF/2026/260/15/OR_ABI-L2-CMIPF-M6C11_G19_s20262601520206_e20262601529514_c20262601530001.nc",
    "ABI-L2-CMIPF/2026/260/15/OR_ABI-L2-CMIPF-M6C13_G19_s20262601520206_e20262601529526_c20262601529570.nc",
    "ABI-L2-CMIPF/2026/260/15/OR_ABI-L2-CMIPF-M6C14_G19_s20262601520206_e20262601529514_c20262601529583.nc",
    "ABI-L2-CMIPF/2026/260/15/OR_ABI-L2-CMIPF-M6C15_G19_s20262601520206_e20262601529520_c20262601529573.nc",
)
#: ``-srcwin`` of the window on the 2 km grid: x offset, y offset, width,
#: height. A finer channel's window is this times its ratio to 2 km.
WINDOW = (3200, 2100, 220, 220)
#: The infrared channels' grid; a channel with more pixels a side is
#: finer by that ratio.
INFRARED_SIDE = 5424
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "goes"


def cut(source: Path, out: Path) -> None:
    info = json.loads(subprocess.run(["gdalinfo", "-json", f'NETCDF:"{source}":CMI'], check=True, capture_output=True, text=True).stdout)
    ratio = info["size"][0] // INFRARED_SIDE
    window = tuple(value * ratio for value in WINDOW)
    subprocess.run(
        [
            "gdal_translate",
            "-q",
            "-of",
            "netCDF",
            # The solar bands' windows are deflated (the infrared ones
            # were cut before and stay byte for byte as committed).
            *(["-co", "FORMAT=NC4", "-co", "COMPRESS=DEFLATE", "-co", "ZLEVEL=9"] if ratio > 1 else []),
            "-srcwin",
            *(str(value) for value in window),
            f'NETCDF:"{source}":CMI',
            str(out),
        ],
        check=True,
    )
    info = json.loads(subprocess.run(["gdalinfo", "-json", f'NETCDF:"{out}":CMI'], check=True, capture_output=True, text=True).stdout)
    wkt = info["coordinateSystem"]["wkt"]
    band = info["bands"][0]
    if "Geostationary" not in wkt or "sweep=x" not in wkt:
        raise SystemExit(f"{out}: the cut lost the geostationary projection")
    unit = "1" if ratio > 1 else "K"
    if band.get("noDataValue") != 65535 or "scale" not in band or "offset" not in band or band.get("unit") != unit:
        raise SystemExit(f"{out}: the cut lost the band's packing: {band}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", type=Path, help="a directory already holding the twelve files")
    args = parser.parse_args(argv)
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for key in KEYS:
        name = key.rsplit("/", 1)[-1]
        source = (args.source or FIXTURE_DIR.parent / "goes-raw") / name
        out = FIXTURE_DIR / name
        if not source.is_file():
            if args.source is not None and out.is_file():
                # A window already committed, whose source was not
                # downloaded again: kept as it is, so widening the fixture
                # by a channel never recuts the others on another GDAL.
                print(f"{out.name}: kept", file=sys.stderr)
                continue
            if args.source is not None:
                raise SystemExit(f"{source} is missing")
            source.parent.mkdir(parents=True, exist_ok=True)
            print(f"downloading {name}", file=sys.stderr)
            urllib.request.urlretrieve(f"{BUCKET}/{key}", source)
        cut(source, out)
        print(f"{out.name}: {out.stat().st_size} bytes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
