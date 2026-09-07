"""Xue v1 quantization codebooks, vectorized with NumPy.

All rounding is round-half-up: ``round(x) = floor(x + 0.5)``. Every rounded
quantity here is non-negative, so this equals round-half-away-from-zero.
Round-half-even (Python's built-in ``round`` and the IEEE 754 default) must
not be used; it changes codes for values landing exactly on a half step.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .errors import ConversionError


@dataclass(frozen=True)
class TemperatureCodebook:
    """Linear uint8 codebook. Not temperature-specific: it quantizes any
    linear field; the wind components reuse it with a symmetric m/s range."""

    minimum: float = -60.0
    maximum: float = 50.0
    step: float = 0.5
    nodata_code: int = 255
    name: str = "temperature"

    @property
    def maximum_code(self) -> int:
        return int(math.floor((self.maximum - self.minimum) / self.step + 0.5))

    def metadata(self) -> dict[str, object]:
        return {
            "type": "linear",
            "offset": self.minimum,
            "scale": self.step,
            "minimumCode": 0,
            "maximumCode": self.maximum_code,
            "nodataCode": self.nodata_code,
        }

    def quantize(self, values: np.ndarray) -> np.ndarray:
        if not np.isfinite(values).all():
            raise ConversionError(f"{self.name} plane contains non-finite values")
        clamped = np.clip(values.astype(np.float64), self.minimum, self.maximum)
        codes = np.floor((clamped - self.minimum) / self.step + 0.5)
        return codes.astype(np.uint8)

    def decode(self, codes: np.ndarray) -> np.ndarray:
        if (codes == self.nodata_code).any():
            raise ConversionError(f"{self.name} plane contains nodata codes")
        if (codes > self.maximum_code).any():
            raise ConversionError(f"{self.name} plane contains invalid codes")
        return self.minimum + codes.astype(np.float64) * self.step


@dataclass(frozen=True)
class PrecipitationCodebook:
    trace: float = 0.01
    scale: float = 0.05
    maximum: float = 128.0
    maximum_code: int = 253
    overflow_code: int = 254
    nodata_code: int = 255

    @property
    def _lo(self) -> float:
        return math.log1p(self.trace / self.scale)

    @property
    def _hi(self) -> float:
        return math.log1p(self.maximum / self.scale)

    @property
    def _span(self) -> int:
        return self.maximum_code - 1

    def metadata(self) -> dict[str, object]:
        return {
            "type": "log1p",
            "trace": self.trace,
            "scale": self.scale,
            "maximum": self.maximum,
            "minimumCode": 1,
            "maximumCode": self.maximum_code,
            "zeroCode": 0,
            "overflowCode": self.overflow_code,
            "nodataCode": self.nodata_code,
        }

    def quantize(self, values: np.ndarray) -> np.ndarray:
        if not np.isfinite(values).all():
            raise ConversionError("precipitation plane contains non-finite values")
        rates = values.astype(np.float64)
        unit = (np.log1p(np.clip(rates, 0.0, self.maximum) / self.scale) - self._lo) / (self._hi - self._lo)
        codes = 1 + np.floor(self._span * unit + 0.5)
        codes = np.clip(codes, 1, self.maximum_code).astype(np.uint8)
        codes[rates < self.trace] = 0
        codes[rates > self.maximum] = self.overflow_code
        return codes

    def decode(self, codes: np.ndarray) -> np.ndarray:
        if (codes == self.nodata_code).any():
            raise ConversionError("precipitation plane contains nodata codes")
        # The overflow code extends the logarithmic grid one step past the
        # maximum, so codes 253 and 254 decode to distinct, increasing values.
        unit = (codes.astype(np.float64) - 1) / self._span
        rates = self.scale * np.expm1(self._lo + unit * (self._hi - self._lo))
        rates[codes == 0] = 0.0
        return rates


QUALITY_TEMPERATURE = TemperatureCodebook()
QUALITY_PRECIPITATION = PrecipitationCodebook()
COMPACT_TEMPERATURE = TemperatureCodebook(step=1.0)
COMPACT_PRECIPITATION = PrecipitationCodebook(maximum_code=125, overflow_code=126, nodata_code=127)
# Each 10 m wind component gets a symmetric linear
# codebook. ±63.5 m/s covers every GFS 10 m wind with headroom (extremes
# clamp like temperature); the 0.5 m/s step (0.25 m/s error budget) is far
# below what a particle animation can resolve.
QUALITY_WIND = TemperatureCodebook(minimum=-63.5, maximum=63.5, step=0.5, name="wind")
COMPACT_WIND = TemperatureCodebook(minimum=-63.5, maximum=63.5, step=1.0, name="wind")
# Surface downward shortwave radiation: 0–1270 W/m²
# covers instantaneous surface DSWRF with headroom (clear-sky maxima stay
# below ~1200; rare cloud-edge enhancement clamps like temperature extremes);
# the 5 W/m² step uses the full 0..254 code space.
QUALITY_FLUX = TemperatureCodebook(minimum=0.0, maximum=1270.0, step=5.0, name="dswrf")
COMPACT_FLUX = TemperatureCodebook(minimum=0.0, maximum=1270.0, step=10.0, name="dswrf")
# Radar composite reflectivity: 0-80 dBZ covers every echo a ground mosaic
# reports (the strongest hail cores reach the mid-70s) and the 0.5 dB step is
# finer than the 5 dB classes a reflectivity palette draws. Code 0 is both
# "no echo" and "no radar coverage": a mosaic is a regional product on a
# rectangular grid, and the format carries no bitmap, so the bottom of the
# range is what a renderer paints as nothing. It is deliberately an ordinary
# linear code, not a reserved one — the shader interpolates codes before the
# palette lookup, and a reserved code between neighbours would colour the gap
# with a class the data never reached.
QUALITY_REFLECTIVITY = TemperatureCodebook(minimum=0.0, maximum=80.0, step=0.5, name="cref")
COMPACT_REFLECTIVITY = TemperatureCodebook(minimum=0.0, maximum=80.0, step=1.0, name="cref")

PROFILES: dict[str, dict[str, TemperatureCodebook | PrecipitationCodebook]] = {
    "quality": {
        "tmp2m": QUALITY_TEMPERATURE,
        "prate": QUALITY_PRECIPITATION,
        "ugrd10m": QUALITY_WIND,
        "vgrd10m": QUALITY_WIND,
        "dswrf": QUALITY_FLUX,
        "cref": QUALITY_REFLECTIVITY,
    },
    "compact": {
        "tmp2m": COMPACT_TEMPERATURE,
        "prate": COMPACT_PRECIPITATION,
        "ugrd10m": COMPACT_WIND,
        "vgrd10m": COMPACT_WIND,
        "dswrf": COMPACT_FLUX,
        "cref": COMPACT_REFLECTIVITY,
    },
    # Production default since 2026-08-17: temperature keeps the 0.5°C step
    # (0.25°C error budget, shared with the H.264 video artifact), while
    # precipitation drops to the 128-level codebook — halving its symbol
    # count cuts the prate bundle by roughly 13% for a codebook step that
    # stays well inside the palette's visual resolution.
    "balanced": {
        "tmp2m": QUALITY_TEMPERATURE,
        "prate": COMPACT_PRECIPITATION,
        "ugrd10m": QUALITY_WIND,
        "vgrd10m": QUALITY_WIND,
        "dswrf": QUALITY_FLUX,
        "cref": QUALITY_REFLECTIVITY,
    },
}
