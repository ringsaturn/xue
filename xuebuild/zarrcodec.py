"""The ``xue.delta`` codec for zarr-python.

An array-to-array codec: along one axis, the first slice is kept whole and
every later slice is stored as its modulo-256 difference against the slice
before it; decoding is a running sum, modulo 256. On ``uint8`` that is
exactly the container's PREVIOUS predictor (docs/format.md, "Temporal
Prediction"), lifted into a Zarr codec chain so a chunk's compressed bytes
come out the same as the bundle's. It is *not* numcodecs' ``Delta``, which
differences the flattened buffer — on a C-ordered ``(time, y, x)`` chunk
that is a spatial difference along x with one stray residual per row.

The exporter (`xuebuild.zarrstore`) never goes through this class: it
writes the differences itself, with NumPy. This module exists so a client
that *reads* a delta-coded store through zarr-python or xarray can register
the codec, which is why it imports zarr-python lazily and is only usable
when the optional ``zarr`` dependency group is installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

CODEC_NAME = "xue.delta"

try:
    from zarr.abc.codec import ArrayArrayCodec
    from zarr.core.common import parse_named_configuration
    from zarr.registry import register_codec
except ImportError:  # zarr-python is optional, by design
    ArrayArrayCodec = None  # type: ignore[assignment,misc]


def available() -> bool:
    """Whether zarr-python is importable, and so whether the codec is."""
    return ArrayArrayCodec is not None


def delta_encode(block: np.ndarray, axis: int = 0) -> np.ndarray:
    """First slice whole, every later slice its wrapping difference."""
    if block.dtype != np.uint8:
        raise TypeError(f"{CODEC_NAME} codes uint8 arrays, not {block.dtype}")
    stored = block.copy()
    ahead = [slice(None)] * block.ndim
    behind = [slice(None)] * block.ndim
    ahead[axis] = slice(1, None)
    behind[axis] = slice(None, -1)
    stored[tuple(ahead)] = (block[tuple(ahead)] - block[tuple(behind)]).astype(np.uint8)
    return stored


def delta_decode(stored: np.ndarray, axis: int = 0) -> np.ndarray:
    """The running sum, modulo 256 — NumPy's uint8 accumulation wraps."""
    if stored.dtype != np.uint8:
        raise TypeError(f"{CODEC_NAME} codes uint8 arrays, not {stored.dtype}")
    return np.cumsum(stored, axis=axis, dtype=np.uint8)


if ArrayArrayCodec is not None:

    @dataclass(frozen=True)
    class XueDeltaCodec(ArrayArrayCodec):  # type: ignore[misc]
        """``{"name": "xue.delta", "configuration": {"axis": 0}}``."""

        is_fixed_size = True

        axis: int = 0

        def __init__(self, *, axis: int = 0) -> None:
            if not isinstance(axis, int) or isinstance(axis, bool) or axis < 0:
                raise TypeError(f"{CODEC_NAME} axis must be a non-negative integer, not {axis!r}")
            object.__setattr__(self, "axis", axis)

        @classmethod
        def from_dict(cls, data: dict[str, Any]) -> XueDeltaCodec:
            _, configuration = parse_named_configuration(data, CODEC_NAME, require_configuration=False)
            return cls(**(configuration or {}))

        def to_dict(self) -> dict[str, Any]:
            return {"name": CODEC_NAME, "configuration": {"axis": self.axis}}

        def validate(self, shape: tuple[int, ...], dtype: Any, chunk_grid: Any) -> None:
            if self.axis >= len(shape):
                raise ValueError(f"{CODEC_NAME} axis {self.axis} is outside a {len(shape)}-dimensional array")
            if np.dtype(dtype.to_native_dtype()) != np.uint8:
                raise ValueError(f"{CODEC_NAME} is defined on uint8, not {dtype}")

        def resolve_metadata(self, chunk_spec: Any) -> Any:
            return chunk_spec

        async def _decode_single(self, chunk_array: Any, chunk_spec: Any) -> Any:
            decoded = delta_decode(np.ascontiguousarray(chunk_array.as_numpy_array()), self.axis)
            return chunk_spec.prototype.nd_buffer.from_numpy_array(decoded)

        async def _encode_single(self, chunk_array: Any, chunk_spec: Any) -> Any:
            encoded = delta_encode(np.ascontiguousarray(chunk_array.as_numpy_array()), self.axis)
            return chunk_spec.prototype.nd_buffer.from_numpy_array(encoded)

        def compute_encoded_size(self, input_byte_length: int, chunk_spec: Any) -> int:
            return input_byte_length

    def register() -> None:
        """Make ``xue.delta`` known to zarr-python, so a store carrying it
        opens. Idempotent."""
        register_codec(CODEC_NAME, XueDeltaCodec)

else:

    def register() -> None:
        raise ImportError(
            f"registering the {CODEC_NAME} codec needs zarr-python: install the optional "
            "'zarr' dependency group (uv sync --group zarr)"
        )
