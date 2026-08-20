#!/usr/bin/env python3
"""Build inputs for the WebCodecs random-access spike.

Encodes the temperature plane (same quantized R8 codes Xue would store)
as a GOP-6 lossless H.264 Annex-B elementary stream, then demuxes it into
per-frame access units so a browser page can feed WebCodecs.VideoDecoder
directly without an MP4 demuxer library (using the `avc: {format: "annexb"}`
decoder config).

Outputs into --out-dir (default data/work/webcodecs_spike, gitignored):
  tmp2m_gop6.h264   raw Annex-B elementary stream
  frame_index.json  per-frame byte offset/length/keyframe flag + codec string
  tmp2m_raw.gray     ground-truth quantized bytes, one byte per pixel per frame,
                     for byte-exact comparison against WebCodecs output

Usage:
    .venv/bin/python scripts/prep_webcodecs_spike.py data/raw/gfs.2026081506 \
        --frames 121
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xue.binconvert import _extract_plane, _grid_info, _prepare_frames  # noqa: E402
from xue.gdal import discover_inputs  # noqa: E402
from xue.quantize import PROFILES, TemperatureCodebook  # noqa: E402

START_CODE_3 = b"\x00\x00\x01"


def find_start_codes(data: bytes) -> list[int]:
    positions = []
    i = 0
    n = len(data)
    while i < n - 2:
        if data[i] == 0 and data[i + 1] == 0:
            if data[i + 2] == 1:
                positions.append(i)
                i += 3
                continue
            if i < n - 3 and data[i + 2] == 0 and data[i + 3] == 1:
                positions.append(i)
                i += 4
                continue
        i += 1
    return positions


def nal_payload_start(data: bytes, pos: int) -> int:
    return pos + (3 if data[pos + 2] == 1 else 4)


def demux_annexb(data: bytes) -> tuple[list[dict], str]:
    positions = find_start_codes(data)
    if not positions:
        raise ValueError("no NAL start codes found in stream")

    frames: list[dict] = []
    sps_payload: bytes | None = None
    au_start = positions[0]

    for idx, pos in enumerate(positions):
        end = positions[idx + 1] if idx + 1 < len(positions) else len(data)
        payload_start = nal_payload_start(data, pos)
        nal_type = data[payload_start] & 0x1F

        if nal_type == 7 and sps_payload is None:
            sps_payload = data[payload_start + 1 : end]

        if nal_type in (1, 5):
            frames.append(
                {
                    "offset": au_start,
                    "length": end - au_start,
                    "keyframe": nal_type == 5,
                }
            )
            au_start = end

    if sps_payload is None or len(sps_payload) < 3:
        raise ValueError("no SPS NAL found; cannot derive codec string")

    profile_idc, constraint_flags, level_idc = sps_payload[0], sps_payload[1], sps_payload[2]
    codec_string = f"avc1.{profile_idc:02x}{constraint_flags:02x}{level_idc:02x}"
    return frames, codec_string


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--variable", default="tmp2m", choices=("tmp2m", "prate"))
    parser.add_argument("--profile", choices=("quality", "compact"), default="quality")
    parser.add_argument("--frames", type=int, default=None)
    parser.add_argument("--gop", type=int, default=6)
    parser.add_argument("--out-dir", type=Path, default=Path("data/work/webcodecs_spike"))
    arguments = parser.parse_args()

    if shutil.which("ffmpeg") is None:
        print("ffmpeg not found on PATH", file=sys.stderr)
        return 1

    codebook = PROFILES[arguments.profile][arguments.variable]
    assert isinstance(codebook, TemperatureCodebook) or arguments.variable == "prate"

    paths = discover_inputs(arguments.input)
    grid = _grid_info(paths[0])
    frames = _prepare_frames(paths, arguments.variable)
    if arguments.frames:
        frames = frames[: arguments.frames]

    out_dir = arguments.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    planes: list[np.ndarray] = []
    for frame in frames:
        values = _extract_plane(frame, grid, out_dir)
        planes.append(codebook.quantize(values))

    raw_path = out_dir / f"{arguments.variable}_raw.gray"
    raw_bytes = b"".join(plane.tobytes() for plane in planes)
    raw_path.write_bytes(raw_bytes)

    h264_path = out_dir / f"{arguments.variable}_gop{arguments.gop}.h264"
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{grid.width}x{grid.height}",
            "-r", "1", "-i", str(raw_path),
            "-c:v", "libx264", "-preset", "slow", "-qp", "0",
            "-bf", "0", "-g", str(arguments.gop), "-sc_threshold", "0",
            "-pix_fmt", "gray",
            "-f", "h264", str(h264_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        print(result.stderr.decode("utf-8", "replace"), file=sys.stderr)
        return 1

    stream_bytes = h264_path.read_bytes()
    frame_entries, codec_string = demux_annexb(stream_bytes)

    if len(frame_entries) != len(planes):
        print(
            f"warning: demuxed {len(frame_entries)} access units but encoded {len(planes)} frames",
            file=sys.stderr,
        )

    index = {
        "variable": arguments.variable,
        "width": grid.width,
        "height": grid.height,
        "frameCount": len(frame_entries),
        "gop": arguments.gop,
        "codecString": codec_string,
        "streamFile": h264_path.name,
        "rawFile": raw_path.name,
        "frames": frame_entries,
    }
    (out_dir / "frame_index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")

    harness_dir = Path(__file__).resolve().parent / "webcodecs_spike"
    for name in ("index.html", "spike.js"):
        shutil.copyfile(harness_dir / name, out_dir / name)

    print(f"wrote {h264_path} ({len(stream_bytes)} bytes)")
    print(f"wrote {raw_path} ({len(raw_bytes)} bytes)")
    print(f"codec string: {codec_string}")
    print(f"frames: {len(frame_entries)}, keyframes: {sum(1 for f in frame_entries if f['keyframe'])}")
    print(f"serve with: python3 -m http.server --directory {out_dir} 8765")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
