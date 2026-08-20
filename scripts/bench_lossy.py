#!/usr/bin/env python3
"""Lossy video-tier experiment for Xue payloads.

Encodes the same quantized R8 code planes Xue stores with LOSSY H.264
(x264, a QP ladder) and AV1 (SVT-AV1, a CRF ladder), then judges every tier
with SCIENTIFIC metrics computed in the decoded physical domain — never
perceptual quality:

- temperature: max/mean absolute error in degC, freezing-point (0 degC) flip
  rate, and invalid-code count (codes outside the codebook's valid range);
- precipitation: dry/wet flip rate (trace threshold), threshold flip rates at
  1 and 10 mm/h, max/p99 relative error over wet points, invalid-code count.

The reference field is the losslessly quantized code plane — i.e. exactly
what production Xue (and the lossless H.264 path) delivers — so the
numbers isolate what the lossy encode adds on top of quantization.

Acceptance defaults (see ACCEPTANCE below) are starting points for the plan's
"tens of KB per frame on mobile" goal, not physical constants: a tier passes
when every metric stays inside them AND the mean frame size is at or under
the size target.

Usage:
    .venv/bin/python scripts/bench_lossy.py data/raw/gfs.2026081506 \
        --output data/work/bench_lossy.json [--frames 121] [--profile balanced]

Requires `ffmpeg` on PATH with libx264 and (optionally) libsvtav1.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xue.binconvert import _extract_plane, _grid_info, _prepare_frames  # noqa: E402
from xue.gdal import discover_inputs  # noqa: E402
from xue.quantize import PROFILES, PrecipitationCodebook, TemperatureCodebook  # noqa: E402

# Mobile target: a few tens of KB per frame.
SIZE_TARGET_KB_PER_FRAME = 64.0

# Scientific acceptance thresholds (starting points, tighten as needed).
ACCEPTANCE = {
    "tmp2m": {
        "invalidCodes": 0,
        "maxAbsErrC": 1.0,
        "freezingFlipRate": 0.002,
    },
    "prate": {
        "invalidCodes": 0,
        "dryWetFlipRate": 0.01,
        "thresholdFlipRate1mm": 0.01,
        "thresholdFlipRate10mm": 0.005,
        "maxRelativeError": 0.25,
    },
}


@dataclass(frozen=True)
class LossyConfig:
    name: str
    encode_args: list[str]


def codec_ladder(gop: int) -> list[LossyConfig]:
    """QP/CRF ladders around the production GOP so random access stays cheap."""
    ladder: list[LossyConfig] = []
    for qp in (4, 8, 12, 16):
        ladder.append(
            LossyConfig(
                name=f"x264_qp{qp}_gop{gop}",
                encode_args=[
                    "-c:v", "libx264", "-preset", "slow", "-qp", str(qp),
                    "-bf", "0", "-g", str(gop), "-sc_threshold", "0", "-pix_fmt", "gray",
                ],
            )
        )
    for crf in (20, 30, 40):
        ladder.append(
            LossyConfig(
                name=f"svtav1_crf{crf}_gop{gop}",
                encode_args=[
                    "-c:v", "libsvtav1", "-preset", "6", "-crf", str(crf),
                    "-svtav1-params", f"keyint={gop}", "-pix_fmt", "gray",
                ],
            )
        )
    return ladder


def run_ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def temperature_metrics(original: np.ndarray, decoded: np.ndarray, codebook: TemperatureCodebook) -> dict:
    invalid = (decoded > codebook.maximum_code) | (decoded == codebook.nodata_code)
    safe = np.minimum(decoded, codebook.maximum_code)
    original_c = codebook.decode(original)
    decoded_c = codebook.minimum + safe.astype(np.float64) * codebook.step
    error = np.abs(decoded_c - original_c)
    freezing_flips = (original_c > 0.0) != (decoded_c > 0.0)
    return {
        "invalidCodes": int(np.count_nonzero(invalid)),
        "maxAbsErrC": float(error.max()),
        "meanAbsErrC": float(error.mean()),
        "p999AbsErrC": float(np.percentile(error, 99.9)),
        "freezingFlipRate": float(np.count_nonzero(freezing_flips) / original.size),
    }


def precipitation_metrics(original: np.ndarray, decoded: np.ndarray, codebook: PrecipitationCodebook) -> dict:
    invalid = decoded > codebook.overflow_code
    safe = np.minimum(decoded, codebook.overflow_code)
    original_rates = codebook.decode(original.copy())
    decoded_rates = codebook.decode(safe.astype(np.uint8).copy())
    dry_wet_flips = (original == 0) != (safe == 0)
    metrics = {
        "invalidCodes": int(np.count_nonzero(invalid)),
        "dryWetFlipRate": float(np.count_nonzero(dry_wet_flips) / original.size),
    }
    for threshold, key in ((1.0, "thresholdFlipRate1mm"), (10.0, "thresholdFlipRate10mm")):
        flips = (original_rates >= threshold) != (decoded_rates >= threshold)
        metrics[key] = float(np.count_nonzero(flips) / original.size)
    wet = original_rates >= 0.1
    if wet.any():
        relative = np.abs(decoded_rates[wet] - original_rates[wet]) / original_rates[wet]
        metrics["maxRelativeError"] = float(relative.max())
        metrics["p99RelativeError"] = float(np.percentile(relative, 99))
    else:
        metrics["maxRelativeError"] = 0.0
        metrics["p99RelativeError"] = 0.0
    return metrics


def accepted(variable_id: str, metrics: dict, kb_per_frame: float) -> bool:
    limits = ACCEPTANCE[variable_id]
    return all(metrics[key] <= limit for key, limit in limits.items()) and kb_per_frame <= SIZE_TARGET_KB_PER_FRAME


def encode_and_score(
    raw_path: Path,
    original: np.ndarray,
    frame_count: int,
    width: int,
    height: int,
    config: LossyConfig,
    variable_id: str,
    codebook: TemperatureCodebook | PrecipitationCodebook,
    work: Path,
) -> dict:
    output_path = work / f"{config.name}.mkv"
    encode_start = time.perf_counter()
    result = run_ffmpeg(
        [
            "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{width}x{height}",
            "-r", "12", "-i", str(raw_path),
            *config.encode_args,
            str(output_path),
        ]
    )
    encode_ms = (time.perf_counter() - encode_start) * 1000
    if result.returncode != 0 or not output_path.exists():
        return {"supported": False, "error": result.stderr.decode("utf-8", "replace")[-2000:]}
    encoded_bytes = output_path.stat().st_size

    decode_start = time.perf_counter()
    decode_result = run_ffmpeg(["-i", str(output_path), "-f", "rawvideo", "-pix_fmt", "gray", "-"])
    decode_ms = (time.perf_counter() - decode_start) * 1000
    output_path.unlink(missing_ok=True)
    if decode_result.returncode != 0:
        return {
            "supported": False,
            "encodedBytes": encoded_bytes,
            "error": decode_result.stderr.decode("utf-8", "replace")[-2000:],
        }
    decoded = np.frombuffer(decode_result.stdout, dtype=np.uint8)
    if decoded.size != original.size:
        return {
            "supported": False,
            "encodedBytes": encoded_bytes,
            "error": f"decoded {decoded.size} samples for {original.size} expected",
        }

    if variable_id == "tmp2m":
        assert isinstance(codebook, TemperatureCodebook)
        metrics = temperature_metrics(original, decoded, codebook)
    else:
        assert isinstance(codebook, PrecipitationCodebook)
        metrics = precipitation_metrics(original, decoded, codebook)

    kb_per_frame = encoded_bytes / frame_count / 1024
    return {
        "supported": True,
        "encodedBytes": encoded_bytes,
        "kbPerFrame": round(kb_per_frame, 1),
        "encodeMs": round(encode_ms, 1),
        "decodeMs": round(decode_ms, 1),
        "decodeMsPerFrame": round(decode_ms / frame_count, 2),
        "metrics": metrics,
        "accepted": accepted(variable_id, metrics, kb_per_frame),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/work/bench_lossy.json"))
    parser.add_argument("--profile", choices=tuple(PROFILES), default="balanced")
    parser.add_argument("--frames", type=int, default=None, help="limit to the first N frames")
    parser.add_argument("--gop", type=int, default=6)
    parser.add_argument("--work-dir", type=Path, default=Path("data/work"))
    arguments = parser.parse_args()

    if shutil.which("ffmpeg") is None:
        print("ffmpeg not found on PATH", file=sys.stderr)
        return 1

    codebooks = PROFILES[arguments.profile]
    paths = discover_inputs(arguments.input)
    grid = _grid_info(paths[0])
    frames = {vid: _prepare_frames(paths, vid) for vid in ("tmp2m", "prate")}
    if arguments.frames:
        frames = {vid: items[: arguments.frames] for vid, items in frames.items()}

    report: dict = {
        "input": str(arguments.input),
        "profile": arguments.profile,
        "grid": f"{grid.width}x{grid.height}",
        "frameCount": len(frames["tmp2m"]),
        "gop": arguments.gop,
        "sizeTargetKbPerFrame": SIZE_TARGET_KB_PER_FRAME,
        "acceptance": ACCEPTANCE,
        "platform": platform.platform(),
        "ffmpegVersion": subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE)
        .stdout.decode()
        .splitlines()[0],
        "variables": {},
    }

    arguments.work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=arguments.work_dir, prefix="bench-lossy-") as temporary:
        work = Path(temporary)
        for variable_id in ("tmp2m", "prate"):
            codebook = codebooks[variable_id]
            planes = [
                codebook.quantize(_extract_plane(frame, grid, work))
                for frame in frames[variable_id]
            ]
            original = np.concatenate([plane.ravel() for plane in planes])
            raw_path = work / f"{variable_id}.gray"
            raw_path.write_bytes(original.tobytes())

            entry: dict = {"rawBytes": int(original.size), "codecs": {}}
            for config in codec_ladder(arguments.gop):
                print(f"[{variable_id}] encoding with {config.name} ...", file=sys.stderr)
                entry["codecs"][config.name] = encode_and_score(
                    raw_path, original, len(planes), grid.width, grid.height, config, variable_id, codebook, work
                )
            report["variables"][variable_id] = entry

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
