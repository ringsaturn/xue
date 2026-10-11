"""Zstandard helpers used by the Xue build pipeline.

A build compresses and read-back-verifies a couple of thousand planes, so the
codec runs in-process through the standard library's ``compression.zstd``
(releasing the GIL inside libzstd) rather than spawning a process per plane.
The module keeps its name for its callers.
"""

from __future__ import annotations

from compression import zstd as _zstd

from .errors import ConversionError

DEFAULT_LEVEL = 15
_MINIMUM_VERSION = (1, 5, 0)
# Bound decoder memory: planes are at most 64M points, and the reference
# encoder's window never exceeds this cap (2**27 bytes = 128 MiB).
_WINDOW_LOG_MAX = 27


def zstd_version() -> tuple[int, ...]:
    version = tuple(_zstd.zstd_version_info)
    if version < _MINIMUM_VERSION:
        raise ConversionError(f"zstd {'.'.join(map(str, version))} is too old, require 1.5.0+")
    return version


def compress(payload: bytes, *, level: int = DEFAULT_LEVEL, checksum: bool = True) -> bytes:
    options = {
        _zstd.CompressionParameter.compression_level: level,
        _zstd.CompressionParameter.checksum_flag: int(checksum),
    }
    return _zstd.compress(payload, options=options)


def decompress(payload: bytes, *, expected_length: int | None = None) -> bytes:
    try:
        output = _zstd.decompress(payload, options={_zstd.DecompressionParameter.window_log_max: _WINDOW_LOG_MAX})
    except _zstd.ZstdError as exc:
        raise ConversionError(f"zstd decompression failed: {exc}") from exc
    if expected_length is not None and len(output) != expected_length:
        raise ConversionError(f"zstd output length {len(output)} does not match expected {expected_length}")
    return output


def frame_has_checksum(payload: bytes) -> bool:
    """Read the Content_Checksum_flag from a Zstandard frame header."""
    if len(payload) < 5 or payload[:4] != b"\x28\xb5\x2f\xfd":
        raise ConversionError("payload is not a Zstandard frame")
    return bool(payload[4] & 0x04)
