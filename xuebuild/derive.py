"""The arithmetic that turns extracted planes into published ones
(mirrored in ``encode/derive.rs``): the derived vectors (vapour flux, wave)
and scalars (equivalent potential temperature, precipitation type, clear-air
turbulence), and the three ways a precipitation input becomes the rate.

Every operation runs in float64 in a fixed order the native encoder repeats
step by step; the two are held byte-identical, so nothing here may be
reordered for style.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np

from .bundles import (
    CAT_SHEAR_LAYERS,
    DERIVED_SCALARS,
    cat_level,
    derivation,
    theta_e_level,
    vector_input_ids,
)
from .errors import ConversionError
from .grid import GridInfo
from .sources import SourceSpec
from .variables import STANDARD_GRAVITY


def precipitation_mode(source: SourceSpec) -> str | None:
    """How the source's precipitation input becomes the published rate:
    ``accumulated`` (a run total, differenced against the previous frame),
    ``averaged`` (a window mean, de-averaged), ``interval`` (the total over
    the step, divided by its length), or None for a rate read as it is."""
    if source.accumulated_precipitation:
        return "accumulated"
    if source.averaged_precipitation:
        return "averaged"
    if source.interval_precipitation:
        return "interval"
    return None


# Above this a temperature the GDAL GRIB driver labels Celsius is kelvin
# (soil_temperature_unit).
SOIL_KELVIN_THRESHOLD = 150.0


def soil_temperature_unit(unit: str, values: np.ndarray) -> str:
    """The unit a soil temperature plane is really in. GDAL's GRIB driver
    relabels every kelvin parameter Celsius but converts only those its
    tables mark for conversion, and the soil temperatures (NCEP's 2/0/2,
    the WMO 2/3/18 ECMWF writes) are not marked: their values come back in
    kelvin under a ``C`` label. A plane labelled Celsius that holds a value
    above 150 is therefore kelvin — no soil is that hot in Celsius, none
    that cold in kelvin — and should a GDAL release convert them after all,
    the values speak for themselves again. The bitmap's fill, the bottom of
    the codebook, is below the threshold either way. Mirrored in the native
    encoder's ``convert_units``."""
    if unit == "C" and values.size and float(values.max()) > SOIL_KELVIN_THRESHOLD:
        return "K"
    return unit


def derive_vapour_flux(
    values: dict[str, np.ndarray], bundle_id: str
) -> tuple[np.ndarray, np.ndarray]:
    """The water vapour flux components on one isobaric surface, q·V/g in
    g·cm⁻¹·hPa⁻¹·s⁻¹, from the specific humidity already in g/kg and the wind
    in m/s there. One multiplication then one division per component, in this
    order, in float64: the native encoder does exactly the same, and the two
    are held byte-identical."""
    specific_humidity_id, u_id, v_id = vector_input_ids(bundle_id)
    q = values[specific_humidity_id]
    return q * values[u_id] / STANDARD_GRAVITY, q * values[v_id] / STANDARD_GRAVITY


def derive_wave_vector(values: dict[str, np.ndarray], bundle_id: str) -> tuple[np.ndarray, np.ndarray]:
    """A wave vector: a significant wave height, already in metres, laid
    along the direction those waves travel, as an eastward and a northward
    component — the combined sea's (``wave``), the wind sea's
    (``windwave``) or the first swell partition's (``swell``), each from
    its own height and direction. Every direction is degrees true the
    waves come *from* (the meteorological convention the wind uses), so
    the components are
    the wind's ``(-h sin θ, -h cos θ)`` and a reader's ``atan2(-u, -v)``
    gives the direction back. Land is 0 m from 0°, so (0, 0). The
    operations run in this order, in float64 — degrees to radians by one
    multiplication, sine and cosine, the negated height times each — and
    the native encoder repeats them, which is what keeps the two
    byte-identical on a field neither reads from a record."""
    height_id, direction_id = vector_input_ids(bundle_id)
    radians = values[direction_id] * (math.pi / 180.0)
    height = -values[height_id]
    return height * np.sin(radians), height * np.cos(radians)


def derive_vector(values: dict[str, np.ndarray], bundle_id: str) -> tuple[np.ndarray, np.ndarray]:
    """The two components of one derived vector bundle, from the planes of
    its inputs."""
    derived = derivation(bundle_id)
    if derived is not None and derived.kind == "wave-vector":
        return derive_wave_vector(values, bundle_id)
    return derive_vapour_flux(values, bundle_id)


# The smallest specific humidity the derivation sees, in kg/kg: a dry
# stratospheric cell can carry zero, whose vapour pressure has no logarithm.
_THETA_E_MINIMUM_Q = 1e-7


def derive_theta_e(values: dict[str, np.ndarray], bundle_id: str) -> np.ndarray:
    """The equivalent potential temperature on one isobaric surface, in K,
    from the temperature already in °C and the specific humidity already in
    g/kg there: Bolton (1980) eq. 43 with its own lifting-condensation-level
    temperature (eq. 15) and the dew point inverted from its eq. 10. The
    operations below run in this exact order, in float64, and the native
    encoder reproduces them one by one — that is what keeps the two
    byte-identical on a field neither reads from a record."""
    temperature_id, humidity_id = DERIVED_SCALARS[bundle_id]
    p = float(theta_e_level(bundle_id))
    t = values[temperature_id] + 273.15
    q = np.maximum(values[humidity_id] / 1000.0, _THETA_E_MINIMUM_Q)
    r = q / (1.0 - q)
    e = p * r / (0.622 + r)
    ln_e = np.log(e / 6.112)
    dew_point = 243.5 * ln_e / (17.67 - ln_e) + 273.15
    t_lcl = 1.0 / (1.0 / (dew_point - 56.0) + np.log(t / dew_point) / 800.0) + 56.0
    theta = t * (1000.0 / p) ** (0.2854 * (1.0 - 0.28 * r))
    r_g = r * 1000.0
    return theta * np.exp((3.376 / t_lcl - 0.00254) * r_g * (1.0 + 0.00081 * r_g))


# GRIB2 code table 4.201's codes for the four types pgrb2's categorical flags
# report, and the order they are combined in: rain, then freezing rain, then
# ice pellets, then snow, each later flag overriding an earlier one. The four
# are mutually exclusive in the model's own output — an analysis has no
# cell with two flags set — so the order only decides a point the
# model should never produce; both encoders share it so even such a point
# encodes byte-identically.
PTYPE_CODES: tuple[tuple[str, float], ...] = (
    ("crain", 1.0),
    ("cfrzr", 3.0),
    ("cicep", 8.0),
    ("csnow", 5.0),
)


def derive_ptype(values: dict[str, np.ndarray], bundle_id: str) -> np.ndarray:
    """The categorical precipitation type on the ground, from the four 0/1
    flags pgrb2 carries: WMO code table 4.201's value for each (1 rain,
    3 freezing rain, 5 snow, 8 ice pellets), 0 where none is set. The flags
    are combined in :data:`PTYPE_CODES` order, each setting the code where
    it is non-zero. Comparisons and selects only, in float64, which the
    native encoder reproduces one by one."""
    inputs = DERIVED_SCALARS[bundle_id]
    codes = np.zeros_like(values[inputs[0]])
    for variable_id, code in PTYPE_CODES:
        codes = np.where(values[variable_id] > 0.0, code, codes)
    return codes


# The clear-air turbulence derivation's constants, each one literal shared
# with the native encoder. The sphere is the mean Earth radius; past 85° the
# zonal spacing shrinks towards nothing and no airway is drawn there; the
# floor keeps a calm cell's logarithm finite. The climatological ln EDR
# mean and standard deviation are Sharman & Pearson's (2017), as ECMWF's
# CAT index (Bechtold et al. 2021, Tech. Memo. 874, c1 = -2.57,
# c2 = 0.51) projects onto them.
EARTH_RADIUS_M = 6371000.0
CAT_POLAR_LATITUDE = 85.0
CAT_TI1_FLOOR = 1e-12
CAT_EDR_LOG_MEAN = -2.572
CAT_EDR_LOG_STD = 0.5067
# ICAO Annex 3 classes an EDR of 0.10 or less as nil turbulence, and the
# shell draws nothing below it; the half of the map that sits in that range
# would otherwise carry most of a bundle's entropy (57 % of its bytes).
CAT_EDR_NIL = 0.10


def ellrod_ti1(
    planes: Mapping[str, np.ndarray],
    level: int,
    latitudes: np.ndarray,
    longitude_step: float,
    latitude_step: float,
    wraps: bool,
) -> tuple[np.ndarray, np.ndarray]:
    """Ellrod & Knapp's (1992) TI1 on one isobaric surface, in s^-2, and the
    mask of the cells it is defined on, from ``(rows, columns)`` planes of
    the wind (m/s) and the geopotential height (gpm) keyed by input id.

    The planes must be the whole grid the source arrives on, north row
    first: a crop's edge or a decimated grid would put the differences on
    the wrong neighbours. ``latitudes`` are the rows' centres in degrees.
    Defined on every row but the first and the last whose latitude is within
    :data:`CAT_POLAR_LATITUDE`, and on every column when the grid wraps (the
    first and last columns being neighbours), every column but the end ones
    when it does not; elsewhere the index is 0.

    In float64, in exactly this order, which the native encoder repeats
    step by step:

    1. ``rad = pi / 180``; per row ``c = cos(latitude * rad)`` (the scalar
       libm cosine, one per row);
    2. per row ``dx = R * c * (longitude_step * rad)``, left to right, and
       ``dy = R * (-latitude_step * rad)``; ``two_dx = 2 * dx``,
       ``two_dy = 2 * dy``;
    3. for f in u, v on the surface: ``df/dx = (f[r, j+1] - f[r, j-1]) / two_dx[r]``
       (columns modulo the width), ``df/dy = (f[r-1, j] - f[r+1, j]) / two_dy``
       (row r-1 is the northern neighbour);
    4. ``DST = du/dx - dv/dy``; ``DSH = dv/dx + du/dy``;
       ``DEF = sqrt(DST * DST + DSH * DSH)``;
    5. ``du = u_above - u_below``; ``dv = v_above - v_below``;
       ``dz = max(z_above - z_below, 1.0)``;
       ``VWS = sqrt(du * du + dv * dv) / dz``;
    6. ``TI1 = VWS * DEF``, then 0 wherever the mask is false.
    """
    above, below = CAT_SHEAR_LAYERS[level]
    u = planes[f"ugrd{level}"]
    v = planes[f"vgrd{level}"]
    rows, columns = u.shape
    if latitude_step >= 0.0:
        raise ConversionError("the clear-air turbulence index needs a north-to-south grid")
    if rows < 3 or latitudes.shape != (rows,):
        raise ConversionError(f"cannot difference a {rows}-row plane against {latitudes.shape[0]} latitudes")
    rad = math.pi / 180.0
    cosine = np.array([math.cos(float(latitude) * rad) for latitude in latitudes], dtype=np.float64)
    dx = EARTH_RADIUS_M * cosine * (longitude_step * rad)
    dy = EARTH_RADIUS_M * (-latitude_step * rad)
    two_dx = (2.0 * dx)[:, None]
    two_dy = 2.0 * dy

    def d_dx(f: np.ndarray) -> np.ndarray:
        return (np.roll(f, -1, axis=1) - np.roll(f, 1, axis=1)) / two_dx

    def d_dy(f: np.ndarray) -> np.ndarray:
        return (np.roll(f, 1, axis=0) - np.roll(f, -1, axis=0)) / two_dy

    dst = d_dx(u) - d_dy(v)
    dsh = d_dx(v) + d_dy(u)
    deformation = np.sqrt(dst * dst + dsh * dsh)
    du = planes[f"ugrd{above}"] - planes[f"ugrd{below}"]
    dv = planes[f"vgrd{above}"] - planes[f"vgrd{below}"]
    dz = np.maximum(planes[f"hgt{above}"] - planes[f"hgt{below}"], 1.0)
    shear = np.sqrt(du * du + dv * dv) / dz
    ti1 = shear * deformation
    valid = np.zeros((rows, columns), dtype=bool)
    valid[1:-1] = True
    valid &= (np.abs(latitudes) <= CAT_POLAR_LATITUDE)[:, None]
    if not wraps:
        valid[:, 0] = False
        valid[:, -1] = False
    return np.where(valid, ti1, 0.0), valid


def derive_cat(
    planes: Mapping[str, np.ndarray],
    bundle_id: str,
    latitudes: np.ndarray,
    longitude_step: float,
    latitude_step: float,
    wraps: bool,
    calibration: tuple[float, float],
) -> np.ndarray:
    """The clear-air turbulence on one isobaric surface as an eddy
    dissipation rate in m^(2/3)/s: :func:`ellrod_ti1` projected from its
    own lognormal fit ``calibration = (mean, standard deviation)`` of ln TI1
    onto the climatological ln EDR distribution (Sharman & Pearson 2017).
    After TI1, in float64 and this order:

    1. ``b = CAT_EDR_LOG_STD / std``; ``a = CAT_EDR_LOG_MEAN - b * mean``
       (scalars);
    2. ``ln_ti1 = ln(max(TI1, CAT_TI1_FLOOR))``;
    3. ``EDR = exp(a + b * ln_ti1)``;
    4. ``EDR = 0`` where ``EDR < CAT_EDR_NIL`` (strictly: 0.10 itself
       keeps its code);
    5. 0 wherever TI1 is undefined — the codebook floor, drawn as no
       turbulence, since a plane carries no nodata.
    """
    level = cat_level(bundle_id)
    if level is None:
        raise ConversionError(f"{bundle_id} is not a clear-air turbulence bundle")
    mean, std = calibration
    ti1, valid = ellrod_ti1(planes, level, latitudes, longitude_step, latitude_step, wraps)
    b = CAT_EDR_LOG_STD / std
    a = CAT_EDR_LOG_MEAN - b * mean
    ln_ti1 = np.log(np.maximum(ti1, CAT_TI1_FLOOR))
    edr = np.exp(a + b * ln_ti1)
    edr = np.where(edr < CAT_EDR_NIL, 0.0, edr)
    return np.where(valid, edr, 0.0)


def uncropped_shape(grid: GridInfo) -> tuple[int, int]:
    """``(rows, columns)`` of a variable's plane before its crop window is
    cut: the published grid's, or the crop's source grid's."""
    if grid.crop is None:
        return grid.height, grid.width
    return grid.crop.source_height, grid.crop.source_width


def uncropped_latitudes(grid: GridInfo) -> np.ndarray:
    """The centre latitude of every row of the uncropped plane, in degrees:
    ``first_latitude + (row - crop_row_start) * latitude_step`` in float64,
    the published grid's own origin and step counted from the crop's first
    row (0 without a crop)."""
    rows, _columns = uncropped_shape(grid)
    row_start = grid.crop.row_start if grid.crop is not None else 0
    return np.array(
        [grid.first_latitude + float(row - row_start) * grid.latitude_step for row in range(rows)],
        dtype=np.float64,
    )


def uncropped_wraps(grid: GridInfo) -> bool:
    """Whether the uncropped plane's columns run all the way round."""
    _rows, columns = uncropped_shape(grid)
    return abs(columns * grid.longitude_step - 360.0) < 1e-6


def derive_cat_plane(
    planes: Mapping[str, np.ndarray],
    bundle_id: str,
    grid: GridInfo,
    calibration: Mapping[int, tuple[float, float]],
) -> np.ndarray:
    """One ``cat<level>`` frame on the published (cropped) grid, derived on
    the uncropped planes and only then cut to the window."""
    level = cat_level(bundle_id)
    assert level is not None
    if grid.projection is not None:
        raise ConversionError(f"{bundle_id} is derived on a regular grid, and this one is on a map projection")
    if level not in calibration:
        raise ConversionError(f"the source publishes {bundle_id} without a ln TI1 calibration for {level} hPa")
    shape = uncropped_shape(grid)
    edr = derive_cat(
        {variable_id: planes[variable_id].reshape(shape) for variable_id in DERIVED_SCALARS[bundle_id]},
        bundle_id,
        uncropped_latitudes(grid),
        grid.longitude_step,
        grid.latitude_step,
        uncropped_wraps(grid),
        calibration[level],
    )
    if grid.crop is not None:
        edr = grid.crop.take(edr)
    return np.ascontiguousarray(edr).ravel()


def derive_scalar(values: dict[str, np.ndarray], bundle_id: str) -> np.ndarray:
    """The one plane of a derived scalar bundle: the equivalent potential
    temperature from the temperature and specific humidity on its surface,
    the categorical precipitation type from the four flags."""
    derived = derivation(bundle_id)
    if derived is not None and derived.kind == "precipitation-type":
        return derive_ptype(values, bundle_id)
    if derived is not None and derived.kind == "theta-e":
        return derive_theta_e(values, bundle_id)
    raise ConversionError(f"{bundle_id} is not a derived scalar")


def deaccumulate_precipitation(
    current_mm: np.ndarray,
    previous_mm: np.ndarray | None,
    step_hours: int,
) -> np.ndarray:
    """Mean precipitation rate (mm/h) over the step ending at the current
    frame, from run-total accumulations in mm. The first frame has no
    preceding interval, so its rate is zero; packing noise can make the
    accumulation dip slightly, so negative differences clamp to zero."""
    if previous_mm is None:
        return np.zeros_like(current_mm)
    return np.maximum(current_mm - previous_mm, 0.0) / step_hours


def interval_rate(total_mm: np.ndarray, step_hours: int) -> np.ndarray:
    """Mean precipitation rate (mm/h) over the step ending at the current
    frame, from the total that fell over that step, in mm.

    The third arrival shape of precipitation, and the simplest: Open-Meteo's
    ``precipitation`` already describes the interval since the model's
    previous native output time, so there is nothing to difference against
    and no predecessor plane to share — one float64 division by the hours
    that interval spans, which is this frame's lead less the previous
    frame's on the axis being built (1, 3 or 6 hours on IFS HRES). The
    operation order around it is the one every source follows
    (:func:`_extract_planes`): the NaN fill first, then unit conversion —
    none, for a variable already in millimetres — and only then this
    division. Mirrored, division for division, in ``encode/convert.rs``."""
    if step_hours <= 0:
        raise ConversionError(f"an interval precipitation total spans {step_hours} hours")
    return total_mm / step_hours


def average_window_start(hour: int, window_hours: int) -> int:
    """First hour of the averaging window whose interval ends at ``hour``.

    GFS interval averages reset every ``window_hours``: f001–f006 average
    from hour 0, f007–f012 from hour 6, and so on."""
    if hour <= 0:
        raise ConversionError("averaged precipitation has no analysis frame")
    return window_hours * ((hour - 1) // window_hours)


def deaverage_precipitation(
    current_average: np.ndarray,
    hour: int,
    previous_average: np.ndarray | None,
    previous_hour: int | None,
    window_hours: int,
) -> np.ndarray:
    """Mean rate (mm/h) over the step ending at ``hour``, from GFS
    window-cumulative average rates (kg/m^2 s, sflux ``PRATE ave``).

    Each frame is scaled to mm accumulated since its window start and
    differenced against the previous frame of the same window; the window's
    first frame (no ``previous_average``) differences against zero. The rate
    divisor is the interval the difference spans, so a mixed-step axis (three
    hours between frames past f120) needs no external step argument."""
    window_start = average_window_start(hour, window_hours)
    accumulated_mm = current_average * 3600.0 * (hour - window_start)
    previous_mm = np.zeros_like(accumulated_mm)
    interval_start = window_start
    if previous_average is not None and previous_hour is not None:
        if not window_start < previous_hour < hour:
            raise ConversionError("previous averaged frame is outside the current averaging window")
        previous_mm = previous_average * 3600.0 * (previous_hour - window_start)
        interval_start = previous_hour
    return deaccumulate_precipitation(accumulated_mm, previous_mm, hour - interval_start)
