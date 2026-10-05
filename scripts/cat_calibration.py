"""Fit the lognormal of the Ellrod TI1 index per model and surface.

The clear-air turbulence bundles (``cat300`` / ``cat250`` / ``cat200``)
project ln TI1 onto the climatological ln EDR distribution, which needs the
mean and standard deviation of ln TI1 as each model resolves it. This script
reads the wind and geopotential height at 300/250/200 hPa of past GFS and
ECMWF IFS runs by ``.idx`` byte range from their public buckets, computes
TI1 with the encoder's own :func:`xuebuild.binconvert.ellrod_ti1`, and
reports the cos(latitude)-weighted mean and standard deviation of
ln(max(TI1, floor)) over every cell the index is defined on (|lat| <= 85°).
The numbers it prints are frozen as ``SourceSpec.cat_calibration``; a refit
changes output bytes and goes through a release.

Not part of the publish path. Needs gdal_translate, and grib_set for the
CCSDS-packed ECMWF messages.

    .venv/bin/python scripts/cat_calibration.py [--cache DIR] [--json OUT]
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xuebuild.binconvert import CAT_SHEAR_LAYERS, CAT_TI1_FLOOR, ellrod_ti1  # noqa: E402
from xuebuild.idx import ecmwf_field_byte_range, field_byte_range  # noqa: E402
from xuebuild.variables import CAT_LEVELS_HPA  # noqa: E402

GFS_BASE = "https://storage.googleapis.com/global-forecast-system"
ECMWF_BASE = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"

# Two years of cycles, one per season and alternating 00/12 UTC, from the
# first months ECMWF open data was on the 0.25° grid. Both models read the
# same runs, so the fits differ by model and not by sample.
RUNS: tuple[tuple[str, str], ...] = (
    ("20240315", "00"),
    ("20240615", "12"),
    ("20240915", "00"),
    ("20241215", "12"),
    ("20250315", "00"),
    ("20250615", "12"),
    ("20250915", "00"),
    ("20251215", "12"),
    ("20260315", "00"),
    ("20260615", "12"),
)
LEADS: tuple[int, ...] = (0, 24, 48)
LEVELS: tuple[int, ...] = tuple(sorted({level for pair in CAT_SHEAR_LAYERS.values() for level in pair}, reverse=True))
FIELDS: tuple[str, ...] = ("ugrd", "vgrd", "hgt")
ROWS, COLUMNS = 721, 1440


def fetch(url: str, byte_range: tuple[int, int] | None = None) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "xue-cat-calibration"})
    if byte_range is not None:
        request.add_header("Range", f"bytes={byte_range[0]}-{byte_range[1]}")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except OSError:
            # The ECMWF bucket answers bursts with 503 Slow Down.
            if attempt == 4:
                raise
            time.sleep(2.0**attempt)
    raise AssertionError


def gfs_messages(date: str, cycle: str, lead: int) -> list[bytes]:
    url = f"{GFS_BASE}/gfs.{date}/{cycle}/atmos/gfs.t{cycle}z.pgrb2.0p25.f{lead:03d}"
    index = fetch(url + ".idx").decode("ascii")
    out = []
    for level in LEVELS:
        for field in FIELDS:
            spans = field_byte_range(index, f":{field.upper()}:{level} mb:")
            out.append(fetch(url, (spans.start, spans.end)))
    return out


def ecmwf_messages(date: str, cycle: str, lead: int) -> list[bytes]:
    url = f"{ECMWF_BASE}/{date}/{cycle}z/ifs/0p25/oper/{date}{cycle}0000-{lead}h-oper-fc.grib2"
    index = fetch(url.removesuffix(".grib2") + ".index").decode("ascii")
    params = {"ugrd": "u", "vgrd": "v", "hgt": "gh"}
    out = []
    for level in LEVELS:
        for field in FIELDS:
            spans = ecmwf_field_byte_range(index, params[field], levtype="pl", levelist=str(level))
            out.append(fetch(url, (spans.start, spans.end)))
    return out


def planes_for(model: str, date: str, cycle: str, lead: int, cache: Path) -> dict[str, np.ndarray]:
    """The nine planes of one frame, north row first, keyed ``ugrd300`` …"""
    raw = cache / f"{model}.{date}{cycle}.f{lead:03d}.f8"
    if not raw.exists():
        messages = (gfs_messages if model == "gfs" else ecmwf_messages)(date, cycle, lead)
        grib = raw.with_suffix(".grib2")
        grib.write_bytes(b"".join(messages))
        if model == "ecmwf":
            simple = raw.with_suffix(".simple.grib2")
            subprocess.run(["grib_set", "-r", "-s", "packingType=grid_simple", str(grib), str(simple)], check=True)
            shutil.move(simple, grib)
        envi = raw.with_suffix(".envi")
        subprocess.run(
            ["gdal_translate", "-q", "-of", "ENVI", "-ot", "Float64", "-co", "INTERLEAVE=BSQ", str(grib), str(envi)],
            check=True,
        )
        info = json.loads(subprocess.run(["gdalinfo", "-json", str(envi)], check=True, capture_output=True).stdout)
        origin_lat, step = info["geoTransform"][3], info["geoTransform"][5]
        if info["size"] != [COLUMNS, ROWS] or step >= 0 or abs(origin_lat - 90.125) > 1e-6:
            raise SystemExit(f"{raw.name}: unexpected grid {info['size']} {info['geoTransform']}")
        shutil.move(envi, raw)
        for leftover in raw.parent.glob(raw.stem + ".*"):
            if leftover != raw:
                leftover.unlink()
    values = np.fromfile(raw, dtype="<f8").reshape(len(LEVELS) * len(FIELDS), ROWS, COLUMNS)
    planes = {}
    for index, (level, field) in enumerate((level, field) for level in LEVELS for field in FIELDS):
        planes[f"{field}{level}"] = values[index]
    return planes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cache", type=Path, default=Path("tmp/cat-calibration"))
    parser.add_argument("--json", type=Path)
    parser.add_argument("--models", default="gfs,ecmwf")
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    latitudes = 90.0 - 0.25 * np.arange(ROWS, dtype=np.float64)
    weights = np.cos(np.radians(latitudes))[:, None]
    report: dict[str, object] = {"runs": [f"{date}{cycle}" for date, cycle in RUNS], "leads": list(LEADS)}
    for model in args.models.split(","):
        frames = [(date, cycle, lead) for date, cycle in RUNS for lead in LEADS]
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda item: planes_for(model, *item, args.cache), frames))
        sums = {level: [0.0, 0.0, 0.0, 0, 0] for level in CAT_LEVELS_HPA}
        per_frame: dict[int, list[float]] = {level: [] for level in CAT_LEVELS_HPA}
        for date, cycle, lead in frames:
            planes = planes_for(model, date, cycle, lead, args.cache)
            for level in CAT_LEVELS_HPA:
                ti1, valid = ellrod_ti1(planes, level, latitudes, 0.25, -0.25, True)
                x = np.log(np.maximum(ti1, CAT_TI1_FLOOR))
                w = np.broadcast_to(weights, x.shape)[valid]
                xv = x[valid]
                total = sums[level]
                total[0] += float(np.sum(w))
                total[1] += float(np.sum(w * xv))
                total[2] += float(np.sum(w * xv * xv))
                total[3] += int(xv.size)
                total[4] += int(np.count_nonzero(ti1[valid] < CAT_TI1_FLOOR))
                per_frame[level].append(float(np.sum(w * xv) / np.sum(w)))
        fits = {}
        for level, (weight, first, second, count, floored) in sums.items():
            mean = first / weight
            std = math.sqrt(second / weight - mean * mean)
            fits[level] = {
                "mean": round(mean, 4),
                "std": round(std, 4),
                "samples": count,
                "floored": floored,
                "frameMeanSpread": round(float(np.std(per_frame[level])), 4),
            }
            print(f"{model:6s} {level} hPa  mu={mean:.4f}  sigma={std:.4f}  n={count}  floored={floored}")
        report[model] = fits
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
