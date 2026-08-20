#!/usr/bin/env python3
"""Lossless video-codec benchmark for Xue payloads.

Tests the format-design conjecture ("would a video codec beat the
custom quantize + zstd pipeline on size?") by re-using the same quantized R8
planes Xue encodes, feeding them to FFmpeg as raw grayscale video, and
comparing lossless-codec output size against the measured Xue numbers.

Every codec run here is lossless (bit-exact round trip is verified against
the quantized planes), so this is an apples-to-apples comparison against the
zstd-based pipeline, which is also lossless at the code level. No lossy
tuning or error-budget analysis is attempted; lossy modes are risky given
the production temperature error already sits at 0.24999 degC against a
0.25 degC budget.

Usage:
    .venv/bin/python scripts/bench_video.py data/raw/gfs.2026081506 \
        --output data/work/bench_video.json [--frames 121]

Requires `ffmpeg` on PATH with ffv1, libx264, libx265, and (optionally)
libsvtav1 encoders.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xue.binconvert import _extract_plane, _grid_info, _prepare_frames  # noqa: E402
from xue.gdal import discover_inputs  # noqa: E402
from xue.quantize import PROFILES, PrecipitationCodebook, TemperatureCodebook  # noqa: E402


@dataclass(frozen=True)
class CodecConfig:
    name: str
    encode_args: list[str]
    container: str


def codec_matrix() -> list[CodecConfig]:
    return [
        CodecConfig(
            name="ffv1",
            encode_args=["-c:v", "ffv1", "-level", "3", "-slices", "16", "-slicecrc", "1", "-pix_fmt", "gray"],
            container="mkv",
        ),
        CodecConfig(
            name="x264_lossless_gop121",
            encode_args=[
                "-c:v", "libx264", "-preset", "slow", "-qp", "0",
                "-bf", "0", "-g", "121", "-pix_fmt", "gray",
            ],
            container="mkv",
        ),
        CodecConfig(
            name="x264_lossless_gop6",
            encode_args=[
                "-c:v", "libx264", "-preset", "slow", "-qp", "0",
                "-bf", "0", "-g", "6", "-sc_threshold", "0", "-pix_fmt", "gray",
            ],
            container="mkv",
        ),
        CodecConfig(
            name="x265_lossless_gop121",
            encode_args=[
                "-c:v", "libx265", "-preset", "medium",
                "-x265-params", "lossless=1:bframes=0:keyint=121:min-keyint=121",
                "-pix_fmt", "gray",
            ],
            container="mkv",
        ),
        CodecConfig(
            name="x265_lossless_gop6",
            encode_args=[
                "-c:v", "libx265", "-preset", "medium",
                "-x265-params", "lossless=1:bframes=0:keyint=6:min-keyint=6",
                "-pix_fmt", "gray",
            ],
            container="mkv",
        ),
        CodecConfig(
            name="svtav1_lossless_gop121",
            encode_args=[
                "-c:v", "libsvtav1", "-preset", "6",
                "-svtav1-params", "lossless=1",
                "-pix_fmt", "gray",
            ],
            container="mkv",
        ),
    ]


def run_ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def encode_and_verify(
    raw_path: Path,
    frame_count: int,
    width: int,
    height: int,
    config: CodecConfig,
    work: Path,
) -> dict:
    output_path = work / f"{config.name}.{config.container}"
    encode_start = time.perf_counter()
    result = run_ffmpeg(
        [
            "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{width}x{height}",
            "-r", "1", "-i", str(raw_path),
            *config.encode_args,
            str(output_path),
        ]
    )
    encode_ms = (time.perf_counter() - encode_start) * 1000
    if result.returncode != 0 or not output_path.exists():
        return {
            "supported": False,
            "error": result.stderr.decode("utf-8", "replace")[-2000:],
        }

    encoded_bytes = output_path.stat().st_size

    decode_start = time.perf_counter()
    decode_result = run_ffmpeg(
        ["-i", str(output_path), "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    )
    decode_ms = (time.perf_counter() - decode_start) * 1000
    if decode_result.returncode != 0:
        return {
            "supported": False,
            "encodedBytes": encoded_bytes,
            "error": decode_result.stderr.decode("utf-8", "replace")[-2000:],
        }

    decoded = decode_result.stdout
    original = raw_path.read_bytes()
    lossless = decoded == original
    output_path.unlink(missing_ok=True)

    return {
        "supported": True,
        "encodedBytes": encoded_bytes,
        "losslessVerified": lossless,
        "encodeMs": encode_ms,
        "decodeMs": decode_ms,
        "decodeMsPerFrame": decode_ms / frame_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/work/bench_video.json"))
    parser.add_argument("--profile", choices=("quality", "compact"), default="quality")
    parser.add_argument("--frames", type=int, default=None, help="limit to the first N frames")
    parser.add_argument("--work-dir", type=Path, default=Path("data/work"))
    arguments = parser.parse_args()

    if shutil.which("ffmpeg") is None:
        print("ffmpeg not found on PATH", file=sys.stderr)
        return 1

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
        "grid": f"{grid.width}x{grid.height}",
        "frameCount": len(frames["tmp2m"]),
        "platform": platform.platform(),
        "ffmpegVersion": subprocess.run(
            ["ffmpeg", "-version"], stdout=subprocess.PIPE
        ).stdout.decode().splitlines()[0],
        "variables": {},
    }

    configs = codec_matrix()

    with tempfile.TemporaryDirectory(dir=arguments.work_dir, prefix="bench-video-") as temporary:
        work = Path(temporary)
        for variable_id, codebook in (("tmp2m", temperature), ("prate", precipitation)):
            planes: list[np.ndarray] = []
            for frame in frames[variable_id]:
                values = _extract_plane(frame, grid, work)
                planes.append(codebook.quantize(values))

            raw_path = work / f"{variable_id}.gray"
            raw_bytes = b"".join(plane.tobytes() for plane in planes)
            raw_path.write_bytes(raw_bytes)

            entry: dict = {
                "rawBytes": len(raw_bytes),
                "codecs": {},
            }
            for config in configs:
                print(f"[{variable_id}] encoding with {config.name} ...", file=sys.stderr)
                entry["codecs"][config.name] = encode_and_verify(
                    raw_path, len(planes), grid.width, grid.height, config, work
                )
            report["variables"][variable_id] = entry

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
