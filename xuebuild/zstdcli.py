"""Zstandard helpers used by the Xue build pipeline.

A build compresses and read-back-verifies a couple of thousand planes, and
the per-call overhead of a zstd subprocess (spawn plus GIL-serialized pipe
copies, ~20 ms each) dominated the bundle-writing phase. When the
interpreter ships ``compression.zstd`` (Python 3.14+) the codec therefore
runs in-process, releasing the GIL inside libzstd. Older interpreters fall
back to the zstd CLI, following the project convention of shelling out to
well-known tools instead of adding binary Python dependencies. Frames from
both engines are standard Zstandard frames with content checksums; the two
are interchangeable on the decode side, though not byte-identical on the
encode side when the libzstd versions differ.
"""

from __future__ import annotations

import re
import shutil
import subprocess

from .errors import ConversionError

try:
    from compression import zstd as _stdlib_zstd  # Python 3.14+
except ImportError:
    _stdlib_zstd = None

DEFAULT_LEVEL = 15
_MINIMUM_VERSION = (1, 5, 0)
# Bound decoder memory: planes are at most 64M points, and the reference
# encoder's window never exceeds this cap (2**27 bytes = 128 MiB).
_MEMORY_LIMIT = 134217728
_WINDOW_LOG_MAX = 27


def require_zstd() -> str:
    resolved = shutil.which("zstd")
    if not resolved:
        raise ConversionError("required command is missing: zstd")
    return resolved


def zstd_version() -> tuple[int, ...]:
    if _stdlib_zstd is not None:
        version = tuple(_stdlib_zstd.zstd_version_info)
    else:
        result = subprocess.run([require_zstd(), "--version"], text=True, capture_output=True, check=False)
        match = re.search(r"v(\d+(?:\.\d+)+)", result.stdout + result.stderr)
        if result.returncode or not match:
            raise ConversionError("could not determine zstd version")
        version = tuple(int(part) for part in match.group(1).split("."))
    if version < _MINIMUM_VERSION:
        raise ConversionError(f"zstd {'.'.join(map(str, version))} is too old, require 1.5.0+")
    return version


def _run(arguments: list[str], payload: bytes, description: str) -> bytes:
    try:
        result = subprocess.run(arguments, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.decode("utf-8", "replace").strip()
        raise ConversionError(f"{description} failed: {details or f'exit status {exc.returncode}'}") from exc
    return result.stdout


def compress(payload: bytes, *, level: int = DEFAULT_LEVEL, checksum: bool = True) -> bytes:
    if _stdlib_zstd is not None:
        options = {
            _stdlib_zstd.CompressionParameter.compression_level: level,
            _stdlib_zstd.CompressionParameter.checksum_flag: int(checksum),
        }
        return _stdlib_zstd.compress(payload, options=options)
    check_flag = "--check" if checksum else "--no-check"
    return _run(
        [require_zstd(), f"-{level}", check_flag, "-q", "-c"],
        payload,
        f"zstd level {level} compression",
    )


def decompress(payload: bytes, *, expected_length: int | None = None) -> bytes:
    if _stdlib_zstd is not None:
        try:
            output = _stdlib_zstd.decompress(
                payload, options={_stdlib_zstd.DecompressionParameter.window_log_max: _WINDOW_LOG_MAX}
            )
        except _stdlib_zstd.ZstdError as exc:
            raise ConversionError(f"zstd decompression failed: {exc}") from exc
    else:
        output = _run([require_zstd(), "-d", f"--memory={_MEMORY_LIMIT}", "-q", "-c"], payload, "zstd decompression")
    if expected_length is not None and len(output) != expected_length:
        raise ConversionError(f"zstd output length {len(output)} does not match expected {expected_length}")
    return output


def frame_has_checksum(payload: bytes) -> bool:
    """Read the Content_Checksum_flag from a Zstandard frame header."""
    if len(payload) < 5 or payload[:4] != b"\x28\xb5\x2f\xfd":
        raise ConversionError("payload is not a Zstandard frame")
    return bool(payload[4] & 0x04)
