"""The four quantities a profile is read for, derived from the published
levels in a fixed operation order.

Everything here is computed from the fixed-point arrays the product
writes, never from the BUFR floats, so a second implementation reading
``<station>.json`` reproduces the same numbers bit for bit. The order of
operations is the contract (``docs/sounding.md`` §7); NumPy is used for
the arithmetic and nothing is vectorised across levels in a way that would
reassociate a sum.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .bufr import MISSING, TROPOPAUSE_BIT

FREEZING_K = 273.15
"""The 0 °C isotherm, in the product's kelvin."""

PW_TOP_PA = 30000
"""Precipitable water is integrated from the surface to 300 hPa: above it
the mixing ratio is a rounding error, and not every sounding reaches the
same top."""

GRAVITY = 9.80665
"""Standard gravity, m/s²; ``kg/m²`` of water is ``mm`` of depth."""

EPSILON = 0.622
"""The ratio of the molecular weights of water vapour and dry air."""


def _floats(values: tuple[int, ...] | list[int], scale: float) -> np.ndarray:
    """A fixed-point array as floats with NaN for missing."""
    array = np.asarray(values, dtype=np.float64)
    return np.where(array == MISSING, np.nan, array / scale)


def saturation_vapour_pressure(temperature_c: np.ndarray | float) -> np.ndarray | float:
    """Bolton (1980) eq. 10, hPa from °C — the same formula and the same
    constants the encoder's equivalent potential temperature uses."""
    return 6.112 * np.exp((17.67 * temperature_c) / (temperature_c + 243.5))


def freezing_level(p: np.ndarray, z: np.ndarray, t: np.ndarray) -> float | None:
    """The height of the 0 °C isotherm, gpm.

    Walking up from the surface (the levels are in descending pressure),
    the first pair of levels that straddles 273.15 K with the lower one at
    or above it fixes the crossing; the height is linear in temperature
    across that pair. A surface already below freezing has no freezing
    level in the sense the product means (there is no warm layer to leave),
    and nor has a profile that never crosses.
    """
    usable = np.flatnonzero(~np.isnan(t) & ~np.isnan(z) & ~np.isnan(p))
    if usable.size < 2:
        return None
    if t[usable[0]] < FREEZING_K:
        return None
    for lower, upper in zip(usable[:-1], usable[1:]):
        if t[lower] >= FREEZING_K > t[upper]:
            span = t[upper] - t[lower]
            if span == 0.0:
                return float(z[lower])
            fraction = (FREEZING_K - t[lower]) / span
            return float(z[lower] + fraction * (z[upper] - z[lower]))
    return None


def precipitable_water(p: np.ndarray, td: np.ndarray) -> float | None:
    """The depth of the water vapour column, mm.

    Only levels carrying both a pressure and a dew point take part, in
    descending pressure, up to the last one at or above 300 hPa — the
    integral is not extrapolated to exactly 300 hPa, so a sounding that
    stops early reports what it measured. Per level: the saturation
    vapour pressure at the dew point (Bolton 1980) is the vapour pressure
    ``e``; the mixing ratio is ``0.622 e / (p − e)``; the specific
    humidity is ``w / (1 + w)``. The column is the trapezoidal integral of
    the specific humidity over pressure, in pascals, divided by standard
    gravity, summed from the surface upwards.
    """
    usable = np.flatnonzero(~np.isnan(p) & ~np.isnan(td) & (p >= PW_TOP_PA))
    if usable.size < 2:
        return None
    pressure_hpa = p[usable] / 100.0
    vapour = np.asarray(saturation_vapour_pressure(td[usable] - FREEZING_K), dtype=np.float64)
    denominator = pressure_hpa - vapour
    if np.any(denominator <= 0.0):
        return None
    mixing = EPSILON * vapour / denominator
    specific = mixing / (1.0 + mixing)
    total = 0.0
    for lower in range(usable.size - 1):
        thickness = p[usable[lower]] - p[usable[lower + 1]]
        if thickness <= 0.0:
            continue
        total += 0.5 * (specific[lower] + specific[lower + 1]) * thickness
    return float(total / GRAVITY)


def lapse_850_500(p: np.ndarray, z: np.ndarray, t: np.ndarray) -> float | None:
    """The mean temperature lapse rate between the 850 and 500 hPa levels,
    K/km, positive when the temperature falls with height. Both levels
    have to be in the sounding within a hectopascal of their nominal
    pressure with a temperature and a height; the product never
    interpolates a standard level that was not reported."""
    lower = _level_at(p, 85000.0)
    upper = _level_at(p, 50000.0)
    if lower is None or upper is None:
        return None
    if np.isnan(t[lower]) or np.isnan(t[upper]) or np.isnan(z[lower]) or np.isnan(z[upper]):
        return None
    thickness_km = (z[upper] - z[lower]) / 1000.0
    if thickness_km <= 0.0:
        return None
    return float((t[lower] - t[upper]) / thickness_km)


def tropopause(z: np.ndarray, sig: np.ndarray) -> float | None:
    """The height of the first level flagged as a tropopause, gpm. A
    sounding may flag more than one (a secondary tropopause above the
    first); the product publishes the lowest, which is the first in
    descending pressure."""
    for position in range(z.size):
        flags = sig[position]
        if np.isnan(flags) or int(flags) == MISSING:
            continue
        if int(flags) & TROPOPAUSE_BIT and not np.isnan(z[position]):
            return float(z[position])
    return None


def _level_at(p: np.ndarray, target_pa: float, tolerance_pa: float = 100.0) -> int | None:
    """The index of the level within ``tolerance`` of a nominal pressure
    (a hectopascal), the nearest one when several qualify."""
    best: int | None = None
    best_gap = tolerance_pa
    for position in range(p.size):
        if np.isnan(p[position]):
            continue
        gap = abs(float(p[position]) - target_pa)
        if gap <= best_gap:
            best, best_gap = position, gap
    return best


def value_at(p: np.ndarray, values: np.ndarray, target_pa: float) -> float | None:
    """One published value at a nominal pressure, for the index's
    headline; None when that level is absent or the value is missing."""
    position = _level_at(p, target_pa)
    if position is None or np.isnan(values[position]):
        return None
    return float(values[position])


def derive(levels: dict[str, Any]) -> dict[str, Any]:
    """The ``derived`` block of one sounding, from its published arrays.

    Heights are rounded to whole geopotential metres, the lapse rate and
    the precipitable water to a tenth — the precision the measurements
    carry, and the precision a reader may rely on.
    """
    p = _floats(levels["p"], 1.0)
    z = _floats(levels["z"], 1.0)
    t = _floats(levels["t"], 100.0)
    td = _floats(levels["td"], 100.0)
    sig = np.asarray(levels["sig"], dtype=np.float64)
    freezing = freezing_level(p, z, t)
    water = precipitable_water(p, td)
    lapse = lapse_850_500(p, z, t)
    top = tropopause(z, sig)
    return {
        "freezingLevel": None if freezing is None else round(freezing),
        "pw": None if water is None else round(water, 1),
        "lapse850_500": None if lapse is None else round(lapse, 1),
        "tropopause": None if top is None else round(top),
    }


def headline(levels: dict[str, Any], derived: dict[str, Any]) -> dict[str, Any]:
    """The index entry's summary of one sounding: the 500 hPa temperature
    and dew point in °C to a tenth (what the marker layer colours by), the
    freezing level, the precipitable water and the level count."""
    p = _floats(levels["p"], 1.0)
    t = _floats(levels["t"], 100.0)
    td = _floats(levels["td"], 100.0)
    t500 = value_at(p, t, 50000.0)
    td500 = value_at(p, td, 50000.0)
    return {
        "t500": None if t500 is None else round(t500 - FREEZING_K, 1),
        "td500": None if td500 is None else round(td500 - FREEZING_K, 1),
        "freezingLevel": derived["freezingLevel"],
        "pw": derived["pw"],
        "levels": len(levels["p"]),
    }
