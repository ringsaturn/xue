"""Xue v1 temporal grouping and residual coding.

Residuals are one-byte modulo-256 wrapping differences. Wrapping subtraction
and addition are lossless for every byte pair, so there is no residual range
check, no signed interpretation, and no RAW fallback.
"""

from __future__ import annotations

import numpy as np

from .errors import ConversionError

GROUP_LENGTH = 6


def encode_residual(current: np.ndarray, base: np.ndarray) -> np.ndarray:
    if current.shape != base.shape:
        raise ConversionError("plane lengths differ")
    return (current.astype(np.uint8) - base.astype(np.uint8)).astype(np.uint8)


def decode_residual(residual: np.ndarray, base: np.ndarray) -> np.ndarray:
    if residual.shape != base.shape:
        raise ConversionError("plane lengths differ")
    return (residual.astype(np.uint8) + base.astype(np.uint8)).astype(np.uint8)


def group_forecast_hours(hours: list[int], group_length: int = GROUP_LENGTH) -> list[list[int]]:
    if hours != sorted(hours) or len(set(hours)) != len(hours):
        raise ConversionError("forecast hours must be unique and ascending")
    return [hours[start : start + group_length] for start in range(0, len(hours), group_length)]


def anchor_hour(group: list[int]) -> int:
    """The anchor is the frame at zero-based index ``floor(n / 2)`` in its group."""
    if not group:
        raise ConversionError("temporal group is empty")
    return group[len(group) // 2]
