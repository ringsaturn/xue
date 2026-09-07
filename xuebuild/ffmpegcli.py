"""FFmpeg CLI wrapper used to build the optional WebCodecs video artifact.

Follows the same shell-out convention as ``zstdcli.py``. Unlike zstd, ffmpeg
is not a hard build dependency: the video artifact is best-effort, so callers
should catch ``ConversionError`` from ``require_ffmpeg`` and skip the video
path rather than fail the whole build.
"""

from __future__ import annotations

import re
import shutil
import subprocess

from .errors import ConversionError


def require_ffmpeg() -> str:
    resolved = shutil.which("ffmpeg")
    if not resolved:
        raise ConversionError("required command is missing: ffmpeg")
    return resolved


def ffmpeg_version() -> str:
    result = subprocess.run([require_ffmpeg(), "-version"], text=True, capture_output=True, check=False)
    match = re.search(r"ffmpeg version (\S+)", result.stdout + result.stderr)
    if result.returncode or not match:
        raise ConversionError("could not determine ffmpeg version")
    return match.group(1)
