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
from .variables import ISOBARIC_LEVELS_HPA, isobaric_variable_id


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

# Sea-level pressure and the pressure-level geopotential heights. Three rules
# fix these numbers:
#
# 1. The step is a fraction of the level's business contour interval and the
#    254 in-range codes cover the global envelope of GFS analyses with margin.
#    PRMSL is the coarsest at 1 hPa against a 4 hPa interval; the upper-air
#    fields get a quarter of that ratio or better.
# 2. **Half-code alignment**: every standard contour value falls exactly
#    halfway between two codes. A contour that coincides with a code value
#    turns each flat pair of cells into a plateau the shader's `fract`
#    test lights up wholesale; offset by half a step, the line always
#    crosses somewhere the code changes. The rule is
#    `(contour - offset) / step ≡ 0.5 (mod 1)`, which needs the step to
#    divide the interval and the offset to sit half a step off it. This is
#    an *encoder* rule: nothing in the container depends on it, and a
#    decoder must not assume it.
# 3. The coverage is fixed, not adapted per run — golden byte-identity, a
#    stable legend, and comparable probe series across runs all need one
#    codebook per variable for all time. A run that exceeds it flattens the
#    extreme core and leaves every other contour untouched.
#
# The `compact` profile doubles the step as everywhere else, which halves the
# code space and gives up rule 2: it is a size experiment, not a profile a
# contour view is drawn from.
_PRESSURE_STEPS: dict[str, tuple[float, float]] = {
    # variable id -> (offset, quality step)
    "prmsl": (870.5, 1.0),
    # 1000 hPa takes a 10 m step rather than the 6 m the other 30 m-interval
    # levels use: its envelope spans 1474 m (the Antarctic ice sheet's
    # extrapolated heights at one end, the Siberian high at the other) and
    # 6 m x 254 leaves no usable margin. 10 divides 30, so rule 2 still holds.
    "hgt1000": (-905.0, 10.0),
    "hgt925": (-249.0, 6.0),
    "hgt850": (423.0, 6.0),
    "hgt700": (1911.0, 6.0),
    "hgt500": (4252.0, 8.0),
    "hgt300": (7505.0, 10.0),
    "hgt250": (8598.0, 12.0),
    "hgt200": (10086.0, 12.0),
}

# The standard contour interval each level is drawn at, in the variable's own
# unit — what rule 2 aligns against, and what the frontend's isoline layer
# draws. Not part of the container: it never reaches a bundle.
CONTOUR_INTERVALS: dict[str, float] = {
    "prmsl": 4.0,
    "hgt1000": 30.0,
    "hgt925": 30.0,
    "hgt850": 30.0,
    "hgt700": 30.0,
    "hgt500": 40.0,
    "hgt300": 120.0,
    "hgt250": 120.0,
    "hgt200": 120.0,
}

# Contours a level draws heavier than the rest, two shapes because the two
# reasons differ. PRMSL emphasises a regular sub-family — every fifth line,
# the 20 hPa grid a surface chart is read on — so it is an interval. 500 hPa
# emphasises two *particular* adjacent contours, the pair the subtropical
# high is defined by (5880 and 5840 gpm, "588" and "584" on a Chinese chart),
# which no interval can express. Both are drawn from the same field the
# ordinary contours are, so they inherit the half-code alignment.
EMPHASIS_INTERVALS: dict[str, float] = {"prmsl": 20.0}
EMPHASIS_CONTOURS: dict[str, tuple[float, ...]] = {"hgt500": (5840.0, 5880.0)}


def _pressure_codebook(variable_id: str, *, compact: bool) -> TemperatureCodebook:
    """One pressure-family codebook.

    The quality profile spends all 254 in-range codes from the registered
    offset; the compact profile covers the same range at twice the step, so
    the coverage a plane clamps to never depends on the profile."""
    offset, step = _PRESSURE_STEPS[variable_id]
    return TemperatureCodebook(
        minimum=offset,
        maximum=offset + step * 254,
        step=step * 2.0 if compact else step,
        name=variable_id,
    )


PRESSURE_VARIABLE_IDS: tuple[str, ...] = tuple(_PRESSURE_STEPS)
QUALITY_PRESSURE = {
    variable_id: _pressure_codebook(variable_id, compact=False)
    for variable_id in PRESSURE_VARIABLE_IDS
}
COMPACT_PRESSURE = {
    variable_id: _pressure_codebook(variable_id, compact=True)
    for variable_id in PRESSURE_VARIABLE_IDS
}

# The filled isobaric families — temperature, relative humidity, specific
# humidity, the wind components and the water vapour flux components on the
# same eight surfaces. None of these is contoured, so none carries the
# half-code rule above; what they share with the pressure family is fixed
# coverage per variable for all time (byte-identical re-encoding, a stable
# legend, comparable point series), and a compact profile at twice the step.
#
# Temperature keeps tmp2m's 0.5 °C step and takes its range per level: the
# low end has to hold the Antarctic winter at every surface, the high end the
# below-ground extrapolation the lowest surfaces take under the Tibetan
# plateau and the Sahara — no single 127-degree window covers both 850 hPa in
# summer and 200 hPa in winter.
_ISOBARIC_TEMPERATURE_RANGES: dict[int, tuple[float, float]] = {
    1000: (-60.0, 60.0),
    925: (-65.0, 50.0),
    850: (-70.0, 45.0),
    700: (-75.0, 35.0),
    500: (-85.0, 15.0),
    300: (-95.0, 0.0),
    250: (-100.0, -5.0),
    200: (-100.0, -10.0),
}
# Specific humidity spans two orders of magnitude between the surface and the
# upper troposphere, so its step follows the level: 0.2 g/kg where a saturated
# tropical boundary layer reaches 30 g/kg, 0.005 g/kg where saturation is a
# fraction of a gram. Every range spends the full 0..254 code space.
_SPECIFIC_HUMIDITY_STEPS: dict[int, float] = {
    1000: 0.2,
    925: 0.2,
    850: 0.1,
    700: 0.1,
    500: 0.02,
    300: 0.01,
    250: 0.005,
    200: 0.005,
}


def _isobaric_temperature_codebook(level_hpa: int, *, compact: bool) -> TemperatureCodebook:
    minimum, maximum = _ISOBARIC_TEMPERATURE_RANGES[level_hpa]
    return TemperatureCodebook(
        minimum=minimum, maximum=maximum, step=1.0 if compact else 0.5, name=isobaric_variable_id("tmp", level_hpa)
    )


def _specific_humidity_codebook(level_hpa: int, *, compact: bool) -> TemperatureCodebook:
    step = _SPECIFIC_HUMIDITY_STEPS[level_hpa]
    return TemperatureCodebook(
        minimum=0.0,
        maximum=step * 254,
        step=step * 2.0 if compact else step,
        name=isobaric_variable_id("spfh", level_hpa),
    )


# Relative humidity: 0–100 % at half a percent, one codebook for every level.
QUALITY_HUMIDITY = TemperatureCodebook(minimum=0.0, maximum=100.0, step=0.5, name="rh")
COMPACT_HUMIDITY = TemperatureCodebook(minimum=0.0, maximum=100.0, step=1.0, name="rh")
# Isobaric wind components: a jet core passes 100 m/s, so the isobaric pair
# takes ±127 m/s at a 1 m/s step — coarser than the 10 m pair's 0.5 m/s, and
# still far below what a colored field or a particle trace resolves.
QUALITY_ISOBARIC_WIND = TemperatureCodebook(minimum=-127.0, maximum=127.0, step=1.0, name="isobaric wind")
COMPACT_ISOBARIC_WIND = TemperatureCodebook(minimum=-127.0, maximum=127.0, step=2.0, name="isobaric wind")
# Water vapour flux components, q·V/g in g·cm⁻¹·hPa⁻¹·s⁻¹: strong transport
# is 20–40, so ±63.5 at a 0.5 step — the 10 m wind's own numbers — covers
# everything but a typhoon core, which clamps.
QUALITY_VAPOUR_FLUX = TemperatureCodebook(minimum=-63.5, maximum=63.5, step=0.5, name="vapour flux")
COMPACT_VAPOUR_FLUX = TemperatureCodebook(minimum=-63.5, maximum=63.5, step=1.0, name="vapour flux")


def _isobaric_codebooks(*, compact: bool) -> dict[str, TemperatureCodebook]:
    books: dict[str, TemperatureCodebook] = {}
    for level in ISOBARIC_LEVELS_HPA:
        books[isobaric_variable_id("tmp", level)] = _isobaric_temperature_codebook(level, compact=compact)
        books[isobaric_variable_id("rh", level)] = COMPACT_HUMIDITY if compact else QUALITY_HUMIDITY
        books[isobaric_variable_id("spfh", level)] = _specific_humidity_codebook(level, compact=compact)
        for family in ("ugrd", "vgrd"):
            books[isobaric_variable_id(family, level)] = COMPACT_ISOBARIC_WIND if compact else QUALITY_ISOBARIC_WIND
        for family in ("uqflx", "vqflx"):
            books[isobaric_variable_id(family, level)] = COMPACT_VAPOUR_FLUX if compact else QUALITY_VAPOUR_FLUX
    return books


QUALITY_ISOBARIC = _isobaric_codebooks(compact=False)
COMPACT_ISOBARIC = _isobaric_codebooks(compact=True)
ISOBARIC_VARIABLE_IDS: tuple[str, ...] = tuple(QUALITY_ISOBARIC)

PROFILES: dict[str, dict[str, TemperatureCodebook | PrecipitationCodebook]] = {
    "quality": {
        "tmp2m": QUALITY_TEMPERATURE,
        "prate": QUALITY_PRECIPITATION,
        "ugrd10m": QUALITY_WIND,
        "vgrd10m": QUALITY_WIND,
        "dswrf": QUALITY_FLUX,
        "cref": QUALITY_REFLECTIVITY,
        **QUALITY_PRESSURE,
        **QUALITY_ISOBARIC,
    },
    "compact": {
        "tmp2m": COMPACT_TEMPERATURE,
        "prate": COMPACT_PRECIPITATION,
        "ugrd10m": COMPACT_WIND,
        "vgrd10m": COMPACT_WIND,
        "dswrf": COMPACT_FLUX,
        "cref": COMPACT_REFLECTIVITY,
        **COMPACT_PRESSURE,
        **COMPACT_ISOBARIC,
    },
    # Production default since 2026-08-17: temperature keeps the 0.5°C step
    # (0.25°C error budget, shared with the H.264 video artifact), while
    # precipitation drops to the 128-level codebook — halving its symbol
    # count cuts the prate bundle by roughly 13% for a codebook step that
    # stays well inside the palette's visual resolution.
    # Relative humidity is the noisiest field published — small-scale
    # structure at every level — and a 0.5 % step costs ~570 KB a frame on
    # the real 850 hPa plane against ~450 KB at 1 %, for a precision no
    # moisture chart reads. Balanced takes the compact (1 %) codebook there.
    "balanced": {
        "tmp2m": QUALITY_TEMPERATURE,
        "prate": COMPACT_PRECIPITATION,
        "ugrd10m": QUALITY_WIND,
        "vgrd10m": QUALITY_WIND,
        "dswrf": QUALITY_FLUX,
        "cref": QUALITY_REFLECTIVITY,
        **QUALITY_PRESSURE,
        **QUALITY_ISOBARIC,
        **{variable_id: COMPACT_HUMIDITY for variable_id in QUALITY_ISOBARIC if variable_id.startswith("rh")},
    },
}
