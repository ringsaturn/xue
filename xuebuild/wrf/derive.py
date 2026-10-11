"""The Xue variables of one output time from WRF's own fields, on the
WRF mass grid (south to north rows). Regridding comes after."""

from __future__ import annotations

import numpy as np

from ..variables import CLOUD_WATER_LEVELS_M, CLOUD_WATER_VARIABLE_IDS

GRAVITY = 9.81
KELVIN = 273.15
#: Gas constant of dry air, J/(kg·K), and the standard lapse rate, K/m, of
#: the sea level reduction.
R_DRY = 287.05
LAPSE_RATE = 0.0065
#: The hub height the ``wind80m`` pair is read at, metres above ground.
WIND_80M_AGL = 80.0
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


def altitude_levels(values: np.ndarray, z_mass: np.ndarray, terrain: np.ndarray, levels: tuple[int, ...]) -> np.ndarray:
    """A field on the mass levels interpolated, column by column, onto
    constant altitudes in metres above sea level: what ``np.interp(levels,
    z_mass[:, j, i], values[:, j, i], right=0)`` gives each column, all
    columns at once. A level between the ground and the lowest mass level
    takes the lowest level's value, one above the top mass level is zero,
    and one under the model terrain is NaN."""
    nz = values.shape[0]
    z = z_mass.reshape(nz, -1)
    v = values.reshape(nz, -1)
    columns = np.arange(z.shape[1])
    out = np.empty((len(levels), z.shape[1]))
    for k, level in enumerate(levels):
        # The first mass level at or above this altitude: 0 under the
        # lowest, nz over the top (heights rise up every column).
        upper = np.sum(z < level, axis=0)
        above = np.clip(upper, 1, nz - 1)
        z0, z1 = z[above - 1, columns], z[above, columns]
        v0, v1 = v[above - 1, columns], v[above, columns]
        interpolated = v0 + (level - z0) / (z1 - z0) * (v1 - v0)
        out[k] = np.where(upper == 0, v[0], np.where(upper == nz, 0.0, interpolated))
    out[np.asarray(levels, dtype=np.float64)[:, None] < terrain.reshape(1, -1)] = np.nan
    return out.reshape((len(levels), *values.shape[1:]))


def above_ground_level(values: np.ndarray, z_mass: np.ndarray, terrain: np.ndarray, height: float) -> np.ndarray:
    """A field on the mass levels interpolated, column by column, onto one
    height above the model ground: linear between the two mass levels that
    bracket it, the lowest level's value under the lowest level and the top
    level's above the top. The lowest mass level sits a few metres to a few
    tens of metres above the ground, so 80 m is bracketed throughout."""
    nz = values.shape[0]
    z = z_mass.reshape(nz, -1) - terrain.reshape(1, -1)
    v = values.reshape(nz, -1)
    columns = np.arange(z.shape[1])
    upper = np.sum(z < height, axis=0)
    above = np.clip(upper, 1, nz - 1)
    z0, z1 = z[above - 1, columns], z[above, columns]
    v0, v1 = v[above - 1, columns], v[above, columns]
    interpolated = v0 + (height - z0) / (z1 - z0) * (v1 - v0)
    out = np.where(upper == 0, v[0], np.where(upper == nz, v[-1], interpolated))
    return out.reshape(values.shape[1:])


def wind_80m(fields: dict[str, np.ndarray], z_mass: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The earth-relative wind 80 m above the ground: ``U`` and ``V``
    destaggered onto the mass points, read at 80 m AGL in every column,
    then turned with the local map rotation."""
    u = above_ground_level(destagger(fields["U"], axis=2), z_mass, fields["HGT"], WIND_80M_AGL)
    v = above_ground_level(destagger(fields["V"], axis=1), z_mass, fields["HGT"], WIND_80M_AGL)
    return rotate_to_earth(u, v, fields["COSALPHA"], fields["SINALPHA"])


def sea_level_pressure(psfc: np.ndarray, t2: np.ndarray, q2: np.ndarray, hgt: np.ndarray) -> np.ndarray:
    """Surface pressure (Pa) reduced to mean sea level, in Pa: the hypsometric
    equation over the terrain height with the column's mean virtual
    temperature taken as the 2 m virtual temperature warmed down the
    standard lapse rate to half the height. A nest is a few tens of
    kilometres across and its sea level pressure is a background field,
    not a product, so the plain reduction serves; WRF's own ``slp``
    (the pressure extrapolated from the model level 100 hPa above the
    ground) would need the 3D state for a difference under a hectopascal."""
    virtual = t2 * (1.0 + 0.608 * q2)
    mean = virtual + LAPSE_RATE * hgt / 2.0
    return psfc * np.exp(GRAVITY * hgt / (R_DRY * mean))


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
    z_mass = mass_heights(fields["PH"], fields["PHB"])
    out.update(cloud_covers(fields["CLDFRA"], z_mass))
    out["hpbl"] = fields["PBLH"]
    out["dswrf"] = fields["SWDOWN"]
    out["orog"] = fields["HGT"]
    out["ugrd80m"], out["vgrd80m"] = wind_80m(fields, z_mass)
    out["prmsl"] = sea_level_pressure(fields["PSFC"], fields["T2"], fields["Q2"], fields["HGT"])
    # Cloud water mixing ratio, kg/kg in WRF, in the registry's g/kg.
    cloud_water = altitude_levels(1000.0 * fields["QCLOUD"], z_mass, fields["HGT"], CLOUD_WATER_LEVELS_M)
    out.update(zip(CLOUD_WATER_VARIABLE_IDS, cloud_water))
    return out
