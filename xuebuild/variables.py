"""The variable registry: what a field is, in GRIB2's own terms.

Every variable — whether it arrives as a GRIB2 record or, like the radar
composite reflectivity, out of a NetCDF observation file — is identified the
way GRIB2 identifies a field: a parameter triple (discipline, category,
number) and a fixed surface (type plus an optional value). That identity is
both what the fetchers match records on and what a bundle's metadata carries
from schema version 3 onwards (docs/format.md), so there is one description
of a variable rather than one per pipeline stage.

The parameter numbers 192-254 in every category, and the surface types
192-254, are GRIB2's local-use ranges: ``dswrf`` (0/4/192) and ECMWF ``tp``
(0/1/193) already live there. Nothing here needs a locally *defined*
parameter of our own, but the format reserves no separate space for one —
a local number is an ordinary number.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VariableSpec:
    id: str
    label: str
    """English label carried in bundle metadata."""
    output_unit: str
    """Unit of the values a bundle's codebook quantizes, carried in metadata."""
    value_range: tuple[int, int]
    grib_element: str = ""
    index_field: str = ""
    excluded_index_phrases: tuple[str, ...] = ()
    ecmwf_param: str = ""
    """The ``param`` value in ECMWF open data .index lines, empty when the
    variable is not fetched from ECMWF."""
    grib2_discipline: int = 0
    grib2_category: int = -1
    grib2_number: int = -1
    grib2_level_type: int = -1
    """Code table 4.5 type of first fixed surface (1 ground/water surface,
    10 entire atmosphere, 103 height above ground)."""
    grib2_level_value: float | None = None
    """First fixed surface value in that surface's own unit; None when the
    surface carries none — GRIB2 encodes that as a missing scale factor and
    value, which is both what "entire atmosphere" means and what ECMWF writes
    for ``tp``, so matching on None also accepts any."""
    grib2_statistical: int | None = None
    """Code table 4.10 statistical process required of the record (0 average,
    1 accumulation); None requires an instantaneous product."""
    grib2_aliases: tuple[tuple[int, int, int], ...] = ()
    """Other ``(discipline, category, number)`` triples that carry this same
    quantity on the same surface, accepted when matching a record and never
    written: the primary triple is the identity a bundle declares. Centres
    disagree on a few codes — ECMWF encodes ``msl`` as plain pressure (0/3/0)
    on the mean sea level surface where NCEP writes PRMSL (0/3/1)."""
    gdal_unit: str = ""
    """Unit string GDAL's GRIB driver reports for this record (it normalizes
    temperatures to Celsius); carried by header-indexed frames and
    cross-checked against a real gdalinfo pass once per run."""

    def parameter_metadata(self) -> dict[str, object]:
        """The variable's GRIB2 identity, as a schema v3 metadata block.

        A fixed surface with no value is written as GRIB2 encodes it: a
        missing scale factor and scaled value, ``null`` in JSON. Every value
        this pipeline publishes is a whole number of the surface's own unit,
        so the scale factor is always 0."""
        block: dict[str, object] = {
            "discipline": self.grib2_discipline,
            "parameterCategory": self.grib2_category,
            "parameterNumber": self.grib2_number,
            "typeOfFirstFixedSurface": self.grib2_level_type,
            "scaleFactorOfFirstFixedSurface": None,
            "scaledValueOfFirstFixedSurface": None,
        }
        if self.grib2_level_value is not None:
            scale_factor, scaled_value = _scaled_surface_value(self.grib2_level_value)
            block["scaleFactorOfFirstFixedSurface"] = scale_factor
            block["scaledValueOfFirstFixedSurface"] = scaled_value
        return block


def _scaled_surface_value(value: float) -> tuple[int, int]:
    """``(scaleFactor, scaledValue)`` with ``value = scaledValue * 10**-scaleFactor``,
    using the smallest scale factor that represents the value exactly."""
    for scale_factor in range(0, 7):
        scaled = value * 10**scale_factor
        if abs(scaled - round(scaled)) < 1e-9:
            return scale_factor, round(scaled)
    raise ValueError(f"fixed surface value is not representable: {value}")


VARIABLES: dict[str, VariableSpec] = {
    "tmp2m": VariableSpec(
        id="tmp2m",
        label="2 meter temperature",
        output_unit="°C",
        value_range=(-60, 50),
        grib_element="TMP",
        index_field=":TMP:2 m above ground:",
        ecmwf_param="2t",
        grib2_category=0,
        grib2_number=0,
        grib2_level_type=103,
        grib2_level_value=2.0,
        gdal_unit="C",
    ),
    "prate": VariableSpec(
        id="prate",
        label="Precipitation rate",
        output_unit="mm/h",
        value_range=(0, 50),
        grib_element="PRATE",
        index_field=":PRATE:surface:",
        excluded_index_phrases=("ave fcst",),
        grib2_category=1,
        grib2_number=7,
        grib2_level_type=1,
        grib2_level_value=0.0,
        gdal_unit="kg/(m^2 s)",
    ),
    # ECMWF open data has no rate field: tp is the run-total precipitation
    # accumulation (metres, ECMWF-local GRIB2 parameter 0/1/193). It is an
    # input-only variable — the converter de-accumulates it into prate and tp
    # itself never reaches a bundle.
    "tp": VariableSpec(
        id="tp",
        label="Total precipitation",
        output_unit="m",
        value_range=(0, 1),
        grib_element="unknown",
        index_field="",
        ecmwf_param="tp",
        grib2_category=1,
        grib2_number=193,
        grib2_level_type=1,
        grib2_statistical=1,
        gdal_unit="-",
    ),
    # GFS sflux has no instantaneous precipitation rate: PRATE arrives as the
    # mean rate over an averaging window that resets every 6 hours. It is an
    # input-only variable — the converter de-averages consecutive frames into
    # prate and prate_ave itself never reaches a bundle.
    "prate_ave": VariableSpec(
        id="prate_ave",
        label="Window-averaged precipitation rate",
        output_unit="kg/m^2s",
        value_range=(0, 1),
        grib_element="PRATE",
        index_field=":PRATE:surface:",
        grib2_category=1,
        grib2_number=7,
        grib2_level_type=1,
        grib2_level_value=0.0,
        grib2_statistical=0,
        gdal_unit="kg/(m^2 s)",
    ),
    # Surface downward shortwave radiation flux (instantaneous), the
    # solar-radiation layer of the sflux source.
    "dswrf": VariableSpec(
        id="dswrf",
        label="Downward shortwave radiation flux",
        output_unit="W/m²",
        value_range=(0, 1270),
        grib_element="DSWRF",
        index_field=":DSWRF:surface:",
        excluded_index_phrases=("ave fcst",),
        grib2_category=4,
        grib2_number=192,
        grib2_level_type=1,
        grib2_level_value=0.0,
        gdal_unit="W/(m^2)",
    ),
    # 10 m wind components, delivered together as the
    # two-variable wind10m bundle and rendered by the GPU particle layer.
    "ugrd10m": VariableSpec(
        id="ugrd10m",
        label="10 meter U wind component",
        output_unit="m/s",
        value_range=(-64, 64),
        grib_element="UGRD",
        index_field=":UGRD:10 m above ground:",
        ecmwf_param="10u",
        grib2_category=2,
        grib2_number=2,
        grib2_level_type=103,
        grib2_level_value=10.0,
        gdal_unit="m/s",
    ),
    "vgrd10m": VariableSpec(
        id="vgrd10m",
        label="10 meter V wind component",
        output_unit="m/s",
        value_range=(-64, 64),
        grib_element="VGRD",
        index_field=":VGRD:10 m above ground:",
        ecmwf_param="10v",
        grib2_category=2,
        grib2_number=3,
        grib2_level_type=103,
        grib2_level_value=10.0,
        gdal_unit="m/s",
    ),
    # Mean sea level pressure. NCEP publishes two reductions; PRMSL
    # (0/3/1) is the same quantity ECMWF calls ``msl`` — encoded there as
    # plain pressure (0/3/0) on the mean sea level surface, hence the alias —
    # so the two sources carry the same field under one identity. MSLET
    # (0/3/192, the NCEP-local Shuell reduction) is a different quantity and
    # is deliberately not registered. Surface 101 is "mean sea level", which
    # carries no value.
    "prmsl": VariableSpec(
        id="prmsl",
        label="Mean sea level pressure",
        output_unit="hPa",
        value_range=(870, 1125),
        grib_element="PRMSL",
        index_field=":PRMSL:mean sea level:",
        ecmwf_param="msl",
        grib2_category=3,
        grib2_number=1,
        grib2_aliases=((0, 3, 0),),
        grib2_level_type=101,
        gdal_unit="Pa",
    ),
    # Radar composite reflectivity: the column maximum of the equivalent
    # reflectivity factor, so its fixed surface is the entire atmosphere
    # (code table 4.5 value 10, which carries no surface value). The only
    # variable not fetched from GRIB — it arrives as a NetCDF observation
    # series (xue/observation.py), so the record-matching fields are empty.
    "cref": VariableSpec(
        id="cref",
        label="Composite radar reflectivity",
        output_unit="dBZ",
        value_range=(0, 80),
        grib2_category=16,
        grib2_number=5,
        grib2_level_type=10,
    ),
    # Three more surface diagnostics, each a GRIB record of its own with no
    # unit conversion. Registered from the GFS pgrb2 set; ECMWF open data
    # carries neighbours rather than equivalents (``10fg`` is the interval
    # *maximum* gust on the 10 m surface, ``tcc`` a 0–1 fraction) and needs
    # its own matching rule before a source lists them, so ``ecmwf_param``
    # stays empty here. Registration is not publication: no source ships
    # these yet (sources.py), and publishing one means widening its input
    # list and recutting tests/fixtures/gfs.*.crop.grib2 alongside.
    #
    # Wind gust: the instantaneous surface gust diagnostic, 0/2/22 on the
    # ground surface (the ``:GUST:surface:`` pgrb2 record).
    "gust": VariableSpec(
        id="gust",
        label="Wind gust",
        output_unit="m/s",
        value_range=(0, 127),
        grib_element="GUST",
        index_field=":GUST:surface:",
        grib2_category=2,
        grib2_number=22,
        grib2_level_type=1,
        grib2_level_value=0.0,
        gdal_unit="m/s",
    ),
    # Total cloud cover over the whole column, 0/6/1 on the entire atmosphere
    # (surface type 10, no value). pgrb2 carries the instantaneous record
    # beside an interval average of the same field; the ``ave fcst`` phrase
    # is excluded exactly as it is for prate, and the identity's missing
    # statistical process rejects the average at the GRIB2 header too.
    "tcdc": VariableSpec(
        id="tcdc",
        label="Total cloud cover",
        output_unit="%",
        value_range=(0, 100),
        grib_element="TCDC",
        index_field=":TCDC:entire atmosphere:",
        excluded_index_phrases=("ave fcst",),
        grib2_category=6,
        grib2_number=1,
        grib2_level_type=10,
        gdal_unit="%",
    ),
    # Surface-based convective available potential energy, 0/7/6 on the
    # ground surface. pgrb2 also carries the 180 mb and 255 mb mixed-layer
    # variants on surface type 108; the ``:CAPE:surface:`` phrase and the
    # surface type pick the surface-based one.
    "cape": VariableSpec(
        id="cape",
        label="Convective available potential energy",
        output_unit="J/kg",
        value_range=(0, 6350),
        grib_element="CAPE",
        index_field=":CAPE:surface:",
        grib2_category=7,
        grib2_number=6,
        grib2_level_type=1,
        grib2_level_value=0.0,
        gdal_unit="J/kg",
    ),
    # Surface visibility, 0/19/0 on the ground surface. GRIB2 carries metres
    # (GFS caps it at ~24 km); the codebook quantizes kilometres.
    "vis": VariableSpec(
        id="vis",
        label="Visibility",
        output_unit="km",
        value_range=(0, 25),
        grib_element="VIS",
        index_field=":VIS:surface:",
        grib2_category=19,
        grib2_number=0,
        grib2_level_type=1,
        grib2_level_value=0.0,
        gdal_unit="m",
    ),
    # 2 m dew point, 0/0/6 on the 2 m surface — the 2 m temperature's own
    # matching and unit rules (GDAL normalizes it to Celsius too). ECMWF open
    # data carries it as ``2d``.
    "dpt2m": VariableSpec(
        id="dpt2m",
        label="2 meter dew point temperature",
        output_unit="°C",
        value_range=(-70, 40),
        grib_element="DPT",
        index_field=":DPT:2 m above ground:",
        ecmwf_param="2d",
        grib2_category=0,
        grib2_number=6,
        grib2_level_type=103,
        grib2_level_value=2.0,
        gdal_unit="C",
    ),
    # NCEP's apparent temperature (heat index above 26.7 °C, wind chill below
    # 10 °C, the air temperature between), 0/0/21 on the 2 m surface. A
    # record of its own rather than a derivation, so it is GFS-only.
    "aptmp2m": VariableSpec(
        id="aptmp2m",
        label="2 meter apparent temperature",
        output_unit="°C",
        value_range=(-90, 60),
        grib_element="APTMP",
        index_field=":APTMP:2 m above ground:",
        grib2_category=0,
        grib2_number=21,
        grib2_level_type=103,
        grib2_level_value=2.0,
        gdal_unit="C",
    ),
    # The cloud layers: three parameters of their own (0/6/3, 0/6/4, 0/6/5),
    # each on its own layer surface (214 low, 224 middle, 234 high — code
    # table 4.5's cloud layers, which carry no value). pgrb2 writes an
    # instantaneous record beside an interval average of each; the average
    # is excluded by phrase and rejected by its statistical process, as for
    # the total.
    "lcdc": VariableSpec(
        id="lcdc",
        label="Low cloud cover",
        output_unit="%",
        value_range=(0, 100),
        grib_element="LCDC",
        index_field=":LCDC:low cloud layer:",
        excluded_index_phrases=("ave fcst",),
        grib2_category=6,
        grib2_number=3,
        grib2_level_type=214,
        gdal_unit="%",
    ),
    "mcdc": VariableSpec(
        id="mcdc",
        label="Middle cloud cover",
        output_unit="%",
        value_range=(0, 100),
        grib_element="MCDC",
        index_field=":MCDC:middle cloud layer:",
        excluded_index_phrases=("ave fcst",),
        grib2_category=6,
        grib2_number=4,
        grib2_level_type=224,
        gdal_unit="%",
    ),
    "hcdc": VariableSpec(
        id="hcdc",
        label="High cloud cover",
        output_unit="%",
        value_range=(0, 100),
        grib_element="HCDC",
        index_field=":HCDC:high cloud layer:",
        excluded_index_phrases=("ave fcst",),
        grib2_category=6,
        grib2_number=5,
        grib2_level_type=234,
        gdal_unit="%",
    ),
}


# The isobaric families: geopotential height, temperature, relative humidity,
# specific humidity, the wind components and the water vapour flux components,
# each on the eight standard isobaric surfaces. Within a family every level is
# the same GRIB2 parameter on the same surface type (100, isobaric), differing
# only in the surface's pressure — so the entries are generated from one table
# of levels rather than written out eight times per family. The level appears
# three ways and all three must agree: hPa in the id and the ``.idx`` phrase
# GFS uses, Pa in the GRIB2 fixed surface. Registration is not publication:
# which levels a source actually ships is ``SourceSpec.bundle_scalar_ids`` and
# ``SourceSpec.bundle_vector_ids``.
ISOBARIC_LEVELS_HPA: tuple[int, ...] = (1000, 925, 850, 700, 500, 300, 250, 200)
HEIGHT_LEVELS_HPA = ISOBARIC_LEVELS_HPA

# The id prefixes of the isobaric families.
ISOBARIC_FAMILIES: tuple[str, ...] = (
    "hgt",
    "tmp",
    "rh",
    "spfh",
    "ugrd",
    "vgrd",
    "uqflx",
    "vqflx",
    "vvel",
    "thetae",
)


def isobaric_variable_id(family: str, level_hpa: int) -> str:
    return f"{family}{level_hpa}"


def height_variable_id(level_hpa: int) -> str:
    return isobaric_variable_id("hgt", level_hpa)


def isobaric_variable(variable_id: str) -> tuple[str, int] | None:
    """``(family, level_hpa)`` of an isobaric variable, or None for anything
    else — including a level that is not registered (``hgt550``)."""
    for family in ISOBARIC_FAMILIES:
        if variable_id.startswith(family) and variable_id[len(family) :].isdigit():
            level = int(variable_id[len(family) :])
            if level in ISOBARIC_LEVELS_HPA:
                return family, level
    return None


# The value range is the level's codebook coverage (xuebuild/quantize.py),
# which is what an observation fill would clamp to.
_HEIGHT_VALUE_RANGES: dict[int, tuple[int, int]] = {
    1000: (-905, 1635),
    925: (-249, 1275),
    850: (423, 1947),
    700: (1911, 3435),
    500: (4252, 6284),
    300: (7505, 10045),
    250: (8598, 11646),
    200: (10086, 13134),
}
# Temperature coverage per level: the low end holds the Antarctic winter at
# every level, the high end the extrapolated below-ground temperatures the
# lowest surfaces take under the Tibetan plateau and the Sahara.
_TEMPERATURE_VALUE_RANGES: dict[int, tuple[int, int]] = {
    1000: (-60, 60),
    925: (-65, 50),
    850: (-70, 45),
    700: (-75, 35),
    500: (-85, 15),
    300: (-95, 0),
    250: (-100, -5),
    200: (-100, -10),
}
# Specific humidity coverage per level, in g/kg: the saturated tropical
# boundary layer at the bottom, a fraction of a gram at the top.
_SPECIFIC_HUMIDITY_VALUE_RANGES: dict[int, tuple[float, float]] = {
    1000: (0.0, 50.8),
    925: (0.0, 50.8),
    850: (0.0, 25.4),
    700: (0.0, 25.4),
    500: (0.0, 5.08),
    300: (0.0, 2.54),
    250: (0.0, 1.27),
    200: (0.0, 1.27),
}
# Equivalent potential temperature coverage per level, in K: each range is
# the level's codebook (127 K at a 0.5 K step), placed so that the warm-moist
# tropical end fits — 850 hPa runs to 357 K, which holds the 99.9th
# percentile of a real analysis — and the Antarctic winter clamps at the
# bottom, as the temperature does. Only 850 hPa is verified against data;
# the others follow the dry potential temperature's rise with height.
_THETA_E_VALUE_RANGES: dict[int, tuple[int, int]] = {
    1000: (235, 362),
    925: (232, 359),
    850: (230, 357),
    700: (235, 362),
    500: (250, 377),
    300: (285, 412),
    250: (295, 422),
    200: (305, 432),
}


def _isobaric_spec(family: str, level_hpa: int) -> VariableSpec:
    common = dict(
        id=isobaric_variable_id(family, level_hpa),
        grib2_level_type=100,
        grib2_level_value=float(level_hpa) * 100.0,
    )
    if family == "hgt":
        return VariableSpec(
            label=f"{level_hpa} hPa geopotential height",
            output_unit="m",
            value_range=_HEIGHT_VALUE_RANGES[level_hpa],
            grib_element="HGT",
            index_field=f":HGT:{level_hpa} mb:",
            ecmwf_param="gh",
            grib2_category=3,
            grib2_number=5,
            gdal_unit="gpm",
            **common,
        )
    if family == "tmp":
        # GDAL normalizes every GRIB temperature to Celsius, isobaric TMP
        # included; the converter accepts K as well.
        return VariableSpec(
            label=f"{level_hpa} hPa temperature",
            output_unit="°C",
            value_range=_TEMPERATURE_VALUE_RANGES[level_hpa],
            grib_element="TMP",
            index_field=f":TMP:{level_hpa} mb:",
            ecmwf_param="t",
            grib2_category=0,
            grib2_number=0,
            gdal_unit="C",
            **common,
        )
    if family == "rh":
        return VariableSpec(
            label=f"{level_hpa} hPa relative humidity",
            output_unit="%",
            value_range=(0, 100),
            grib_element="RH",
            index_field=f":RH:{level_hpa} mb:",
            ecmwf_param="r",
            grib2_category=1,
            grib2_number=1,
            gdal_unit="%",
            **common,
        )
    if family == "spfh":
        # GRIB2 carries kg/kg; the codebook quantizes g/kg.
        low, high = _SPECIFIC_HUMIDITY_VALUE_RANGES[level_hpa]
        return VariableSpec(
            label=f"{level_hpa} hPa specific humidity",
            output_unit="g/kg",
            value_range=(int(low), int(high)),
            grib_element="SPFH",
            index_field=f":SPFH:{level_hpa} mb:",
            ecmwf_param="q",
            grib2_category=1,
            grib2_number=0,
            gdal_unit="kg/kg",
            **common,
        )
    if family in ("ugrd", "vgrd"):
        element = "UGRD" if family == "ugrd" else "VGRD"
        return VariableSpec(
            label=f"{level_hpa} hPa {'U' if family == 'ugrd' else 'V'} wind component",
            output_unit="m/s",
            value_range=(-127, 127),
            grib_element=element,
            index_field=f":{element}:{level_hpa} mb:",
            ecmwf_param="u" if family == "ugrd" else "v",
            grib2_category=2,
            grib2_number=2 if family == "ugrd" else 3,
            gdal_unit="m/s",
            **common,
        )
    if family == "vvel":
        # Vertical velocity in pressure coordinates, ω = dp/dt: negative is
        # ascent. pgrb2 carries it on every registered level; ECMWF open
        # data as ``w``.
        return VariableSpec(
            label=f"{level_hpa} hPa vertical velocity",
            output_unit="Pa/s",
            value_range=(-6, 6),
            grib_element="VVEL",
            index_field=f":VVEL:{level_hpa} mb:",
            ecmwf_param="w",
            grib2_category=2,
            grib2_number=8,
            gdal_unit="Pa/s",
            **common,
        )
    if family == "thetae":
        # Equivalent potential temperature, Bolton (1980) eq. 43, derived by
        # the converter from the temperature and the specific humidity on
        # the same surface (binconvert.derive_theta_e), never fetched, so
        # the record-matching fields stay empty. GRIB2's own number for the
        # quantity is 0/0/3 (EPOT).
        return VariableSpec(
            label=f"{level_hpa} hPa equivalent potential temperature",
            output_unit="K",
            value_range=_THETA_E_VALUE_RANGES[level_hpa],
            grib2_category=0,
            grib2_number=3,
            **common,
        )
    # Water vapour flux, q·V/g in g·cm⁻¹·hPa⁻¹·s⁻¹ — the unit a Chinese
    # synoptic chart contours it in. Derived by the converter from the
    # specific humidity and the wind on the same surface, never fetched, so
    # the record-matching fields stay empty. GRIB2 has no standard parameter
    # for a per-level horizontal vapour flux; 250 / 251 are local-use numbers
    # of our own in the moisture category, clear of every NCEP and ECMWF
    # local number this pipeline meets.
    assert family in ("uqflx", "vqflx")
    return VariableSpec(
        label=f"{level_hpa} hPa {'U' if family == 'uqflx' else 'V'} water vapour flux component",
        output_unit="g/(cm·hPa·s)",
        value_range=(-64, 64),
        grib2_category=1,
        grib2_number=250 if family == "uqflx" else 251,
        **common,
    )


for _family in ISOBARIC_FAMILIES:
    for _level in ISOBARIC_LEVELS_HPA:
        VARIABLES[isobaric_variable_id(_family, _level)] = _isobaric_spec(_family, _level)
del _family, _level

# Standard gravity, the g in q·V/g.
STANDARD_GRAVITY = 9.80665


def variable_spec(variable_id: str) -> VariableSpec:
    try:
        return VARIABLES[variable_id]
    except KeyError as exc:
        raise ValueError(f"unsupported variable: {variable_id}") from exc
