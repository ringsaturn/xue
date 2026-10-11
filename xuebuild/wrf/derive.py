"""The Xue variables of one output time from WRF's own fields, on the
WRF mass grid (south to north rows). Regridding comes after."""

from __future__ import annotations

import numpy as np

GRAVITY = 9.81
KELVIN = 273.15
# The same Bolton (1980) constants binconvert.derive_theta_e inverts the
# dew point with, and the same floor on a dry cell's mixing ratio.
_MINIMUM_Q = 1e-7
#: Cloud layer bounds in metres above sea level: low below 2 km, middle to
#: 6 km, high above — the WMO étages the GRIB low/middle/high covers follow.
CLOUD_LAYERS = ((0.0, 2000.0), (2000.0, 6000.0), (6000.0, np.inf))


def destagger(values: np.ndarray, axis: int) -> np.ndarray:
    """A staggered field averaged onto the mass points along ``axis``."""
    lower = [slice(None)] * values.ndim
    upper = [slice(None)] * values.ndim
    lower[axis] = slice(None, -1)
    upper[axis] = slice(1, None)
    return 0.5 * (values[tuple(lower)] + values[tuple(upper)])


def rotate_to_earth(u: np.ndarray, v: np.ndarray, cosalpha: np.ndarray, sinalpha: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Grid-relative wind components turned earth-relative with WRF's
    local map rotation."""
    return u * cosalpha - v * sinalpha, v * cosalpha + u * sinalpha


def mass_heights(ph: np.ndarray, phb: np.ndarray) -> np.ndarray:
    """Height above sea level of each mass level, from the geopotential on
    the w levels around it."""
    z_w = (ph + phb) / GRAVITY
    return destagger(z_w, axis=0)


def dew_point(q2: np.ndarray, psfc: np.ndarray) -> np.ndarray:
    """2 m dew point in °C from the 2 m mixing ratio (kg/kg) and the surface
    pressure (Pa): the vapour pressure, then Bolton (1980) eq. 10 inverted."""
    q = np.maximum(q2, _MINIMUM_Q)
    e = q * psfc / (0.622 + q) / 100.0
    ln_e = np.log(e / 6.112)
    return 243.5 * ln_e / (17.67 - ln_e)


def cloud_covers(cldfra: np.ndarray, z_mass: np.ndarray) -> dict[str, np.ndarray]:
    """Total, low, middle and high cloud cover in percent. A layer's cover
    is the largest fraction of any mass level inside it (maximum overlap;
    an empty layer is clear); the total assumes random overlap of the
    column's levels."""
    clear = np.prod(1.0 - cldfra, axis=0)
    covers = {"tcdc": 100.0 * (1.0 - clear)}
    for variable_id, (bottom, top) in zip(("lcdc", "mcdc", "hcdc"), CLOUD_LAYERS):
        inside = (z_mass >= bottom) & (z_mass < top)
        covers[variable_id] = 100.0 * np.max(np.where(inside, cldfra, 0.0), axis=0)
    return covers


def rain_total(fields: dict[str, np.ndarray]) -> np.ndarray:
    """The run's accumulated precipitation in mm: grid-scale, cumulus and
    shallow-cumulus together."""
    return fields["RAINNC"] + fields["RAINC"] + fields["RAINSH"]


def derive(fields: dict[str, np.ndarray], previous_rain: np.ndarray) -> dict[str, np.ndarray]:
    """Every published variable of one output hour, on the mass grid, in
    the registry's units. ``previous_rain`` is :func:`rain_total` of the
    hour before, so ``apcp`` is this hour's fall."""
    u10, v10 = rotate_to_earth(fields["U10"], fields["V10"], fields["COSALPHA"], fields["SINALPHA"])
    out = {
        "tmp2m": fields["T2"] - KELVIN,
        "dpt2m": dew_point(fields["Q2"], fields["PSFC"]),
        "tmpsfc": fields["TSK"] - KELVIN,
        "ugrd10m": u10,
        "vgrd10m": v10,
        "apcp": rain_total(fields) - previous_rain,
    }
    out.update(cloud_covers(fields["CLDFRA"], mass_heights(fields["PH"], fields["PHB"])))
    out["hpbl"] = fields["PBLH"]
    out["dswrf"] = fields["SWDOWN"]
    out["orog"] = fields["HGT"]
    return out
