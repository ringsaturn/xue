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
    numeric_id: int | None = None
    """The container's registered ``variableId`` (docs/format.md), or None for
    an input-only variable that never reaches a bundle (ECMWF ``tp``, sflux
    ``prate_ave``)."""
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
        numeric_id=1,
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
        numeric_id=2,
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
        numeric_id=5,
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
        numeric_id=3,
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
        numeric_id=4,
        grib_element="VGRD",
        index_field=":VGRD:10 m above ground:",
        ecmwf_param="10v",
        grib2_category=2,
        grib2_number=3,
        grib2_level_type=103,
        grib2_level_value=10.0,
        gdal_unit="m/s",
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
        numeric_id=6,
        grib2_category=16,
        grib2_number=5,
        grib2_level_type=10,
    ),
}


def variable_spec(variable_id: str) -> VariableSpec:
    try:
        return VARIABLES[variable_id]
    except KeyError as exc:
        raise ValueError(f"unsupported variable: {variable_id}") from exc
