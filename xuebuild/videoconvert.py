"""Optional lossless-video artifacts, decoded in-browser via WebCodecs.

Encodes the same quantized R8 codes Xue stores as a GOP-6
lossless H.264 Annex-B elementary stream. This is the production port of the
encode/demux logic proven out in ``scripts/prep_webcodecs_spike.py`` and
``scripts/webcodecs_spike/spike.js``: browsers whose ``VideoDecoder`` supports the emitted
profile (Chrome, as of the spike; not Safari) can decode this stream
byte-exact with no WASM and no MP4 demuxer, using
``avc: {format: "annexb"}`` and feeding these access units directly as
``EncodedVideoChunk``s.

This module never fails the build: callers should treat a missing ``ffmpeg``
(or any encode failure) as "skip the video artifact," since Xue remains
the universal fallback.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import ConversionError
from .ffmpegcli import require_ffmpeg


@dataclass(frozen=True)
class VideoArtifact:
    stream_bytes: bytes
    index: dict[str, Any]
    codec_string: str


def _find_start_codes(data: bytes) -> list[int]:
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


def _nal_start(data: bytes, pos: int) -> int:
    return pos + (3 if data[pos + 2] == 1 else 4)


def _demux_annexb(data: bytes) -> tuple[list[dict[str, Any]], str]:
    positions = _find_start_codes(data)
    if not positions:
        raise ConversionError("video encode produced no NAL start codes")

    frames: list[dict[str, Any]] = []
    sps_payload: bytes | None = None
    au_start = positions[0]

    for index, pos in enumerate(positions):
        end = positions[index + 1] if index + 1 < len(positions) else len(data)
        nal_start = _nal_start(data, pos)
        nal_type = data[nal_start] & 0x1F

        if nal_type == 7 and sps_payload is None:
            sps_payload = data[nal_start + 1 : end]

        if nal_type in (1, 5):
            frames.append({"offset": au_start, "length": end - au_start, "keyframe": nal_type == 5})
            au_start = end

    if sps_payload is None or len(sps_payload) < 3:
        raise ConversionError("video encode produced no SPS NAL; cannot derive codec string")

    profile_idc, constraint_flags, level_idc = sps_payload[0], sps_payload[1], sps_payload[2]
    codec_string = f"avc1.{profile_idc:02x}{constraint_flags:02x}{level_idc:02x}"
    return frames, codec_string


def build_debug_playlist(frames: list[dict[str, Any]], stream_uri: str, *, fps: float = 12.0) -> str:
    """Debug-only HLS playlist over the raw Annex-B stream.

    One ``#EXT-X-BYTERANGE`` segment per GOP, all pointing into the single
    ``.h264`` file, so VLC/ffprobe can open the hosted stream directly (the
    picture is the grayscale code plane). Never consumed by the frontend.
    """
    segments: list[tuple[int, int]] = []
    start = 0
    for index, frame in enumerate(frames):
        if frame["keyframe"] and index > start:
            segments.append((start, index))
            start = index
    segments.append((start, len(frames)))

    lines = [
        "#EXTM3U",
        "# debug playlist; open with:",
        "#   ffprobe -allowed_extensions ALL -extension_picky 0 <this url>",
        "#EXT-X-VERSION:4",
        f"#EXT-X-TARGETDURATION:{max(1, round(max(end - begin for begin, end in segments) / fps))}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for begin, end in segments:
        offset = frames[begin]["offset"]
        length = frames[end - 1]["offset"] + frames[end - 1]["length"] - offset
        lines.append(f"#EXTINF:{(end - begin) / fps:.6f},F{begin:03d}-F{end - 1:03d}")
        lines.append(f"#EXT-X-BYTERANGE:{length}@{offset}")
        lines.append(stream_uri)
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def encode_variable_video(
    codes_by_hour: dict[int, dict[str, np.ndarray]],
    hours: list[int],
    variable_id: str,
    *,
    width: int,
    height: int,
    gop: int = 6,
) -> VideoArtifact:
    """Encode one variable's RAW-quantized planes already computed by
    ``binconvert.convert_bin`` as a GOP-6 lossless H.264 Annex-B stream.

    Raises ``ConversionError`` (via ``require_ffmpeg`` or an encode failure)
    if ffmpeg is unavailable or the encode fails; callers should catch this
    and skip the video artifact rather than fail the whole build.
    """
    planes = [codes_by_hour[hour][variable_id] for hour in hours]
    raw_bytes = b"".join(plane.tobytes() for plane in planes)

    command = [
        require_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{width}x{height}",
        "-r", "1", "-i", "pipe:0",
        "-c:v", "libx264", "-preset", "slow", "-qp", "0",
        "-bf", "0", "-g", str(gop), "-sc_threshold", "0",
        "-pix_fmt", "gray",
        "-f", "h264", "pipe:1",
    ]
    result = subprocess.run(command, input=raw_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        details = result.stderr.decode("utf-8", "replace").strip()[-2000:]
        raise ConversionError(f"ffmpeg lossless video encode failed: {details or f'exit status {result.returncode}'}")
    stream_bytes = result.stdout

    frame_entries, codec_string = _demux_annexb(stream_bytes)
    if len(frame_entries) != len(planes):
        raise ConversionError(f"video demux produced {len(frame_entries)} access units for {len(planes)} frames")

    index = {
        "codecString": codec_string,
        "width": width,
        "height": height,
        "gop": gop,
        "frameCount": len(frame_entries),
        "frames": frame_entries,
    }
    return VideoArtifact(stream_bytes=stream_bytes, index=index, codec_string=codec_string)
