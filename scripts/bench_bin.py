#!/usr/bin/env python3
"""Xue benchmark harness.

Reproduces the format-design benchmark measurements: per-variable
compressed sizes for RAW, PREVIOUS-chain, and middle-ANCHOR modes across
temporal group sizes, quantization error percentiles, extreme-value counts,
and single-plane decode timings. Emits one JSON document.

Usage:
    .venv/bin/python scripts/bench_bin.py data/raw/gfs.2026081506 \
        --output data/work/bench_bin.json [--frames 121] [--groups 6 12]
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xue import temporal, zstdcli  # noqa: E402
from xue.binconvert import _extract_plane, _grid_info, _prepare_frames  # noqa: E402
from xue.gdal import discover_inputs  # noqa: E402
from xue.quantize import PROFILES, PrecipitationCodebook, TemperatureCodebook  # noqa: E402


def compressed_size(payload: bytes, level: int) -> int:
    return len(zstdcli.compress(payload, level=level))


def error_percentiles(errors: np.ndarray) -> dict[str, float]:
    return {
        f"p{percentile}": float(np.percentile(errors, percentile))
        for percentile in (50, 90, 99, 100)
    }


def decode_timings(payloads: list[bytes], expected_length: int) -> dict[str, float]:
    samples = []
    for payload in payloads:
        start = time.perf_counter()
        zstdcli.decompress(payload, expected_length=expected_length)
        samples.append((time.perf_counter() - start) * 1000)
    samples.sort()
    return {
        "p50Ms": statistics.median(samples),
        "p95Ms": samples[min(len(samples) - 1, int(len(samples) * 0.95))],
        "maxMs": samples[-1],
        "note": "zstd CLI subprocess decode, includes process startup; WASM timings are measured in the browser",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/work/bench_bin.json"))
    parser.add_argument("--profile", choices=("quality", "compact"), default="quality")
    parser.add_argument("--level", type=int, default=zstdcli.DEFAULT_LEVEL)
    parser.add_argument("--frames", type=int, default=None, help="limit to the first N frames")
    parser.add_argument("--groups", type=int, nargs="+", default=[6, 12])
    parser.add_argument("--work-dir", type=Path, default=Path("data/work"))
    arguments = parser.parse_args()

    codebooks = PROFILES[arguments.profile]
    temperature = codebooks["tmp2m"]
    precipitation = codebooks["prate"]
    assert isinstance(temperature, TemperatureCodebook)
    assert isinstance(precipitation, PrecipitationCodebook)

    paths = discover_inputs(arguments.input)
    grid = _grid_info(paths[0])
    frames = {vid: _prepare_frames(paths, vid) for vid in ("tmp2m", "prate")}
    if arguments.frames:
        frames = {vid: items[: arguments.frames] for vid, items in frames.items()}

    arguments.work_dir.mkdir(parents=True, exist_ok=True)
    import tempfile

    report: dict = {
        "input": str(arguments.input),
        "profile": arguments.profile,
        "zstdLevel": arguments.level,
        "zstdVersion": ".".join(map(str, zstdcli.zstd_version())),
        "grid": f"{grid.width}x{grid.height}",
        "frameCount": len(frames["tmp2m"]),
        "platform": platform.platform(),
        "variables": {},
    }

    with tempfile.TemporaryDirectory(dir=arguments.work_dir, prefix="bench-") as temporary:
        work = Path(temporary)
        for variable_id, codebook in (("tmp2m", temperature), ("prate", precipitation)):
            planes: list[np.ndarray] = []
            errors = []
            extremes = 0
            for frame in frames[variable_id]:
                values = _extract_plane(frame, grid, work)
                codes = codebook.quantize(values)
                planes.append(codes)
                if variable_id == "tmp2m":
                    assert isinstance(codebook, TemperatureCodebook)
                    in_range = (values >= codebook.minimum) & (values <= codebook.maximum)
                    errors.append(np.abs(codebook.decode(codes) - values)[in_range])
                    extremes += int(np.count_nonzero(~in_range))
                else:
                    assert isinstance(codebook, PrecipitationCodebook)
                    extremes += int(np.count_nonzero(values > codebook.maximum))

            hours = [frame.forecast_hour for frame in frames[variable_id]]
            raw_payloads = [zstdcli.compress(plane.tobytes(), level=arguments.level) for plane in planes]
            plane_lookup = dict(zip(hours, planes))
            entry: dict = {
                "rawBytes": len(planes) * planes[0].size,
                "rawCompressedBytes": sum(len(payload) for payload in raw_payloads),
                "extremePoints": extremes,
                "decode": decode_timings(raw_payloads[: min(20, len(raw_payloads))], planes[0].size),
                "modes": {},
            }
            if errors:
                entry["absErrorPercentiles"] = error_percentiles(np.concatenate(errors))

            for group_length in arguments.groups:
                groups = temporal.group_forecast_hours(hours, group_length)
                previous_total = 0
                anchor_total = 0
                for group in groups:
                    anchor = temporal.anchor_hour(group)
                    anchor_plane = plane_lookup[anchor]
                    previous_total += len(zstdcli.compress(plane_lookup[group[0]].tobytes(), level=arguments.level))
                    for earlier, hour in zip(group[:-1], group[1:]):
                        residual = temporal.encode_residual(plane_lookup[hour], plane_lookup[earlier])
                        previous_total += len(zstdcli.compress(residual.tobytes(), level=arguments.level))
                    for hour in group:
                        if hour == anchor:
                            anchor_total += len(zstdcli.compress(anchor_plane.tobytes(), level=arguments.level))
                        else:
                            residual = temporal.encode_residual(plane_lookup[hour], anchor_plane)
                            anchor_total += len(zstdcli.compress(residual.tobytes(), level=arguments.level))
                entry["modes"][str(group_length)] = {
                    "previousChainBytes": previous_total,
                    "middleAnchorBytes": anchor_total,
                }
            report["variables"][variable_id] = entry

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
