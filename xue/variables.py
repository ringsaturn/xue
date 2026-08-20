from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VariableSpec:
    id: str
    label: str
    output_unit: str
    value_range: tuple[int, int]
    grib_element: str
    index_field: str
    excluded_index_phrases: tuple[str, ...] = ()
    ecmwf_param: str = ""
    """The ``param`` value in ECMWF open data .index lines, empty when the
    variable is not fetched from ECMWF."""
    grib2_discipline: int = 0
    grib2_category: int = -1
    grib2_number: int = -1
    grib2_level_type: int = -1
    grib2_level_value: float | None = None
    """First fixed surface value; None accepts any (ECMWF encodes tp's
    surface value as missing)."""
    grib2_statistical: int | None = None
    """Code table 4.10 statistical process required of the record (0 average,
    1 accumulation); None requires an instantaneous product."""
    gdal_unit: str = ""
    """Unit string GDAL's GRIB driver reports for this record (it normalizes
    temperatures to Celsius); carried by header-indexed frames and
    cross-checked against a real gdalinfo pass once per run."""


VARIABLES: dict[str, VariableSpec] = {
    "tmp2m": VariableSpec(
        id="tmp2m",
        label="2 米气温",
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
        label="降水强度",
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
        label="累计降水量",
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
        label="窗口平均降水率",
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
        label="太阳辐射",
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
        label="10 米风 U 分量",
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
        label="10 米风 V 分量",
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
}


def variable_spec(variable_id: str) -> VariableSpec:
    try:
        return VARIABLES[variable_id]
    except KeyError as exc:
        raise ValueError(f"unsupported variable: {variable_id}") from exc
