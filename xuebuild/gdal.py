from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import ConversionError
from .model import SourceFrame
from .variables import SURFACE_TEMPERATURE_IDS, isobaric_variable, variable_spec


SUPPORTED_EXTENSIONS = {".grb", ".grb2", ".grib2"}
HEIGHT_RE = re.compile(r"(?:^|[^0-9])2(?:\.0+)?\s*m(?:eter)?s?\s+above\s+ground", re.IGNORECASE)
TEN_METRE_RE = re.compile(r"(?:^|[^0-9])10(?:\.0+)?\s*m(?:eter)?s?\s+above\s+ground", re.IGNORECASE)


def require_command(command: str) -> str:
    resolved = shutil.which(command)
    if not resolved:
        raise ConversionError(f"required command is missing: {command}")
    return resolved


def run_command(arguments: list[str], *, description: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(arguments, check=True, text=True, capture_output=True)
    except FileNotFoundError as exc:
        raise ConversionError(f"required command is missing: {arguments[0]}") from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        raise ConversionError(f"{description} failed: {details or f'exit status {exc.returncode}'}") from exc


def _native_gdal_info() -> Any:
    """The `xuepy` wheel's `gdal_info`, or None when this build must shell out.

    The wheel carries its own GDAL, so a build converting through the native
    encoder can inspect its inputs without a system `gdalinfo` on PATH — the
    inspection pass was the last thing in the fetch-and-convert path that
    needed one. Which source a build uses follows `XUE_ENCODER`, so a run
    never mixes two GDAL builds: `python` shells out (it needs a system GDAL
    for `gdal_translate` regardless), `native` insists on the wheel, and
    `auto` takes the wheel when it is installed.

    A wheel predating `gdal_info` falls back to the CLI rather than failing:
    the two are held to the same output by `tests/test_gdalinfo.py`.
    """
    # Imported here, not at module scope: `encoder` imports `binconvert`,
    # which imports this module.
    from .encoder import selection  # noqa: PLC0415
    from . import native  # noqa: PLC0415

    requested = selection()
    if requested == "python":
        return None
    if requested == "native":
        # Fail at the first inspection rather than after fetching a whole run.
        return getattr(native.require(), "gdal_info", None)
    if not native.available():
        return None
    return getattr(native.require(), "gdal_info", None)


def dataset_info(name: str | Path, *, description: str) -> dict[str, Any]:
    """What `gdalinfo -json` reports for one dataset, as a dictionary.

    `name` is a GDAL connection string rather than necessarily a path: the
    observation ingest passes `NETCDF:"file.nc":cref`.
    """
    native_info = _native_gdal_info()
    if native_info is not None:
        try:
            return native_info(str(name))
        except Exception as exc:
            raise ConversionError(f"{description} failed: {exc}") from exc
    result = run_command(
        [require_command("gdalinfo"), "-json", str(name)], description=description
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ConversionError(f"GDAL returned invalid JSON for {name}") from exc


def _metadata(band: dict[str, Any]) -> dict[str, str]:
    domains = band.get("metadata", {})
    if not isinstance(domains, dict):
        return {}
    default = domains.get("", domains)
    if not isinstance(default, dict):
        return {}
    return {str(key): str(value) for key, value in default.items()}


def _timestamp(metadata: dict[str, str], key: str) -> datetime | None:
    value = metadata.get(key)
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(float(value)), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def normalize_unit(unit: str) -> str:
    compact = unit.strip().strip("[]()").lower().replace("°", "").replace("degrees", "").replace("degree", "")
    compact = " ".join(compact.split())
    aliases = {
        "k": "K",
        "kelvin": "K",
        "c": "C",
        "celsius": "C",
        "degc": "C",
        "f": "F",
        "fahrenheit": "F",
        "degf": "F",
    }
    if compact not in aliases:
        raise ConversionError(f"unsupported temperature unit: {unit or '<missing>'}")
    return aliases[compact]


def celsius_expression(unit: str, *, low: int = -60, high: int = 50) -> str:
    """A temperature in Celsius, from whichever of K / C / F the record
    reports, clamped to a codebook's range — the 2 m temperature's by
    default; the dew point and the apparent temperature pass their own."""
    normalized = normalize_unit(unit)
    if normalized == "K":
        value = "A-273.15"
    elif normalized == "F":
        value = "(A-32)*5/9"
    else:
        value = "A"
    return f"maximum({low},minimum({high},{value}))"


def precipitation_expression(unit: str) -> str:
    compact = re.sub(r"[\s*()\[\]]", "", unit.strip().lower()).replace("²", "^2")
    aliases = {
        "kg/m^2s",
        "kg/m2s",
        "kgm^-2s^-1",
        "kgm-2s-1",
    }
    if compact not in aliases:
        raise ConversionError(f"unsupported precipitation rate unit: {unit or '<missing>'}")
    return "maximum(0,minimum(50,A*3600))"


def averaged_precipitation_expression(unit: str) -> str:
    """GFS sflux PRATE arrives as the mean rate (kg/m^2 s) over an averaging
    window. The expression is identity — the converter turns window averages
    into per-step rates itself."""
    compact = re.sub(r"[\s*()\[\]]", "", unit.strip().lower()).replace("²", "^2")
    aliases = {
        "kg/m^2s",
        "kg/m2s",
        "kgm^-2s^-1",
        "kgm-2s-1",
    }
    if compact not in aliases:
        raise ConversionError(f"unsupported precipitation rate unit: {unit or '<missing>'}")
    return "A"


def flux_expression(unit: str) -> str:
    """Radiative flux in W/m^2, clamped to the dswrf codebook range."""
    compact = re.sub(r"[\s*()\[\]]", "", unit.strip().lower()).replace("²", "^2")
    aliases = {"w/m^2", "w/m2", "wm^-2", "wm-2"}
    if compact not in aliases:
        raise ConversionError(f"unsupported radiative flux unit: {unit or '<missing>'}")
    return "maximum(0,minimum(1270,A))"


def wind_expression(unit: str) -> str:
    compact = re.sub(r"[\s*()\[\]]", "", unit.strip().lower())
    aliases = {"m/s", "m/sec", "ms-1", "ms^-1", "mps"}
    if compact not in aliases:
        raise ConversionError(f"unsupported wind component unit: {unit or '<missing>'}")
    return "maximum(-64,minimum(64,A))"


def gust_expression(unit: str) -> str:
    """Wind gust: a speed in the wind components' unit, one-sided, clamped
    to the gust codebook's range."""
    compact = re.sub(r"[\s*()\[\]]", "", unit.strip().lower())
    aliases = {"m/s", "m/sec", "ms-1", "ms^-1", "mps"}
    if compact not in aliases:
        raise ConversionError(f"unsupported wind gust unit: {unit or '<missing>'}")
    return "maximum(0,minimum(127,A))"


def cloud_cover_expression(unit: str) -> str:
    """Cloud cover in percent — as pgrb2 carries it, or scaled up from the
    0–1 fraction ECMWF writes (GDAL spells that unit "-")."""
    compact = unit.strip().strip("[]()").lower()
    if compact == "%":
        return "maximum(0,minimum(100,A))"
    if compact in {"-", "1", "fraction", "proportion", "(0 - 1)", "0-1"}:
        return "maximum(0,minimum(100,A*100))"
    raise ConversionError(f"unsupported cloud cover unit: {unit or '<missing>'}")


def visibility_expression(unit: str) -> str:
    """Visibility: GRIB2 carries metres, the codebook quantizes kilometres.
    Only metres are accepted — a file already in km would divide twice, and
    no source publishes one."""
    compact = re.sub(r"[\s*()\[\]]", "", unit.strip().lower())
    if compact not in {"m", "metre", "meter", "metres", "meters"}:
        raise ConversionError(f"unsupported visibility unit: {unit or '<missing>'}")
    return "A/1000"


def reflectivity_expression(unit: str) -> str:
    """Composite reflectivity in dBZ, which GDAL's GRIB tables spell "dB"
    and the radar mosaic's NetCDF spells in full; clamped to the cref
    codebook's range. Sub-zero returns are below the codebook — the mosaic
    treats them as no echo, and so does this."""
    compact = re.sub(r"[\s*()\[\]]", "", unit.strip().lower())
    if compact not in {"db", "dbz"}:
        raise ConversionError(f"unsupported reflectivity unit: {unit or '<missing>'}")
    return "maximum(0,minimum(80,A))"


def vertical_velocity_expression(unit: str) -> str:
    """Vertical velocity in pressure coordinates, already in Pa/s."""
    compact = re.sub(r"[\s*()\[\]]", "", unit.strip().lower())
    if compact not in {"pa/s", "pas-1", "pas^-1"}:
        raise ConversionError(f"unsupported vertical velocity unit: {unit or '<missing>'}")
    return "maximum(-6.35,minimum(6.35,A))"


def cape_expression(unit: str) -> str:
    """Convective available potential energy in J/kg, clamped to the cape
    codebook's range."""
    compact = re.sub(r"[\s*()\[\]]", "", unit.strip().lower())
    aliases = {"j/kg", "jkg-1", "jkg^-1"}
    if compact not in aliases:
        raise ConversionError(f"unsupported CAPE unit: {unit or '<missing>'}")
    return "maximum(0,minimum(6350,A))"


def ice_cover_expression(unit: str) -> str:
    """Sea ice cover: GRIB2 carries a 0–1 proportion (GDAL spells the unit
    "Proportion"), the codebook quantizes percent."""
    compact = re.sub(r"[\s*()\[\]]", "", unit.strip().lower())
    if compact not in {"proportion", "fraction", "1", "-", ""}:
        raise ConversionError(f"unsupported sea ice cover unit: {unit or '<missing>'}")
    return "maximum(0,minimum(100,A*100))"


def ice_thickness_expression(unit: str) -> str:
    """Sea ice thickness in metres, clamped to the icetk codebook range."""
    compact = re.sub(r"[\s*()\[\]]", "", unit.strip().lower())
    if compact not in {"m", "metre", "meter", "metres", "meters"}:
        raise ConversionError(f"unsupported sea ice thickness unit: {unit or '<missing>'}")
    return "maximum(0,minimum(5.08,A))"


def wave_height_expression(unit: str) -> str:
    """Significant wave height in metres, clamped to the htsgw codebook range."""
    compact = re.sub(r"[\s*()\[\]]", "", unit.strip().lower())
    if compact not in {"m", "metre", "meter", "metres", "meters"}:
        raise ConversionError(f"unsupported wave height unit: {unit or '<missing>'}")
    return "maximum(0,minimum(25.4,A))"


def wave_period_expression(unit: str) -> str:
    """Wave period in seconds, clamped to the perpw codebook range."""
    compact = re.sub(r"[\s*()\[\]]", "", unit.strip().lower())
    if compact not in {"s", "sec", "second", "seconds"}:
        raise ConversionError(f"unsupported wave period unit: {unit or '<missing>'}")
    return "maximum(0,minimum(25.4,A))"


def wave_direction_expression(unit: str) -> str:
    """Wave direction in degrees true (GDAL spells it "Degree true"); the
    converter reduces it modulo 360 itself."""
    compact = re.sub(r"[\s*()\[\]]", "", unit.strip().lower())
    if compact not in {"degreetrue", "degtrue", "degrees", "degree", "deg", "degreestrue"}:
        raise ConversionError(f"unsupported wave direction unit: {unit or '<missing>'}")
    return "A"


def accumulation_expression(unit: str) -> str:
    """ECMWF tp arrives in metres of accumulated water; GDAL reports the
    ECMWF-local parameter's unit as "-". The expression is identity — the
    converter de-accumulates consecutive planes itself."""
    compact = unit.strip().strip("[]()").lower()
    if compact not in {"-", "m", ""}:
        raise ConversionError(f"unsupported precipitation accumulation unit: {unit or '<missing>'}")
    return "A"


def pressure_expression(unit: str) -> str:
    """Mean sea level pressure. GRIB2 carries it in pascals and the codebook
    quantizes hectopascals, so this is the one pressure-family field with a
    unit conversion. Only Pa is accepted: a file already in hPa would divide
    twice, and no source publishes one."""
    compact = unit.strip().strip("[]()")
    if compact != "Pa":
        raise ConversionError(f"unsupported pressure unit: {unit or '<missing>'}")
    return "A/100"


def height_expression(unit: str) -> str:
    """Geopotential height, already in metres. GDAL reports GFS HGT as
    "gpm" (geopotential metres); a source spelling it "m" means the same
    number to within the difference between geopotential and geometric
    height, which at these levels is far below the codebook step."""
    compact = unit.strip().strip("[]()")
    if compact not in {"gpm", "m"}:
        raise ConversionError(f"unsupported geopotential height unit: {unit or '<missing>'}")
    return "A"


def humidity_expression(unit: str) -> str:
    """Relative humidity, already in percent."""
    compact = unit.strip().strip("[]()")
    if compact != "%":
        raise ConversionError(f"unsupported relative humidity unit: {unit or '<missing>'}")
    return "maximum(0,minimum(100,A))"


def specific_humidity_expression(unit: str) -> str:
    """Specific humidity: GRIB2 carries a mass ratio (kg/kg), the codebook
    quantizes g/kg. Only kg/kg is accepted — a file already in g/kg would
    scale twice, and no source publishes one."""
    compact = re.sub(r"[\s*()\[\]]", "", unit.strip().lower())
    if compact != "kg/kg":
        raise ConversionError(f"unsupported specific humidity unit: {unit or '<missing>'}")
    return "A*1000"


def isobaric_expression(variable_id: str, unit: str) -> str:
    """The unit rule of one isobaric-family record, by family. Temperature
    takes the 2 m temperature's rule (GDAL normalizes every GRIB temperature
    to Celsius), the wind components the 10 m pair's; the flux components are
    derived and never arrive as a record."""
    family, _level = isobaric_variable(variable_id) or (None, None)
    if family == "hgt":
        return height_expression(unit)
    if family == "tmp":
        return celsius_expression(unit)
    if family == "rh":
        return humidity_expression(unit)
    if family == "spfh":
        return specific_humidity_expression(unit)
    if family in ("ugrd", "vgrd"):
        return wind_expression(unit)
    if family == "vvel":
        return vertical_velocity_expression(unit)
    raise ConversionError(f"unsupported variable: {variable_id}")


def raster_expression(variable_id: str, unit: str) -> str:
    if variable_id == "tmp2m":
        return celsius_expression(unit)
    if variable_id == "prate":
        return precipitation_expression(unit)
    if variable_id == "prate_ave":
        return averaged_precipitation_expression(unit)
    if variable_id == "tp":
        return accumulation_expression(unit)
    if variable_id == "dswrf":
        return flux_expression(unit)
    if variable_id in ("ugrd10m", "vgrd10m"):
        return wind_expression(unit)
    if variable_id == "prmsl":
        return pressure_expression(unit)
    if variable_id == "gust":
        return gust_expression(unit)
    if variable_id == "tcdc":
        return cloud_cover_expression(unit)
    if variable_id == "cape":
        return cape_expression(unit)
    if variable_id == "vis":
        return visibility_expression(unit)
    if variable_id == "cref":
        return reflectivity_expression(unit)
    if variable_id == "dpt2m":
        return celsius_expression(unit, low=-70, high=40)
    if variable_id == "aptmp2m":
        return celsius_expression(unit, low=-90, high=60)
    if variable_id in ("lcdc", "mcdc", "hcdc"):
        return cloud_cover_expression(unit)
    if variable_id == "tmpsfc":
        return celsius_expression(unit, low=-60, high=67)
    if variable_id == "icec":
        return ice_cover_expression(unit)
    if variable_id == "icetk":
        return ice_thickness_expression(unit)
    if variable_id == "htsgw":
        return wave_height_expression(unit)
    if variable_id == "perpw":
        return wave_period_expression(unit)
    if variable_id == "dirpw":
        return wave_direction_expression(unit)
    if isobaric_variable(variable_id) is not None:
        return isobaric_expression(variable_id, unit)
    raise ConversionError(f"unsupported variable: {variable_id}")


def _is_two_metre_record(metadata: dict[str, str], description: str, element: str) -> bool:
    """One GRIB element on the 2 m surface: the temperature, the dew point,
    the apparent temperature."""
    if metadata.get("GRIB_ELEMENT", "").upper() != element:
        return False
    short_name = metadata.get("GRIB_SHORT_NAME", "").upper()
    searchable = " ".join(
        [
            short_name,
            metadata.get("GRIB_COMMENT", ""),
            metadata.get("GRIB_LEVEL", ""),
            description,
        ]
    )
    return short_name in {"2-HTGL", "2-M-HTGL"} or bool(HEIGHT_RE.search(searchable))


def _is_surface_precipitation_rate(metadata: dict[str, str], description: str) -> bool:
    if metadata.get("GRIB_ELEMENT", "").upper() != "PRATE":
        return False
    short_name = metadata.get("GRIB_SHORT_NAME", "").upper()
    searchable = " ".join(
        [
            short_name,
            metadata.get("GRIB_COMMENT", ""),
            metadata.get("GRIB_LEVEL", ""),
            description,
        ]
    ).lower()
    return short_name == "0-SFC" or "surface" in searchable


def _is_total_precipitation(metadata: dict[str, str], description: str) -> bool:
    """ECMWF open data tp: GRIB2 discipline 0, category 1, local parameter
    193 — GDAL's tables do not know it, so GRIB_ELEMENT is "unknown" and the
    comment carries the raw triple."""
    if metadata.get("GRIB_ELEMENT", "").lower() not in {"unknown", "tp", "apcp"}:
        return False
    short_name = metadata.get("GRIB_SHORT_NAME", "").upper()
    comment = metadata.get("GRIB_COMMENT", "")
    searchable = " ".join([comment, metadata.get("GRIB_LEVEL", ""), description]).lower()
    return (short_name == "0-SFC" or "surface" in searchable) and (
        "cat 1, subcat 193" in comment or "total precipitation" in searchable
    )


# The elements GDAL names another centre's spelling of a surface quantity
# by, accepted beside the registry's own: ECMWF's skin temperature is SKINT
# where pgrb2's surface TMP is the same field, its peak wave period PWPER
# and its mean wave direction WWSDIR stand in for WAVEWATCH III's PERPW and
# DIRPW (the registry's alias triples).
_SURFACE_ELEMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "TMP": ("SKINT",),
    "PERPW": ("PWPER",),
    "DIRPW": ("WWSDIR",),
}


def _is_surface_record(metadata: dict[str, str], description: str, element: str) -> bool:
    """One GRIB element on the ground or water surface (DSWRF, GUST, CAPE,
    the surface temperature, the sea ice and wave fields). The fetched files
    carry only the instantaneous surface record of each, so element +
    surface level is unambiguous — the interval averages and the mixed-layer
    CAPE variants are never downloaded. GDAL spells the surface ``0-SFC``;
    the GFS-Wave records carry a surface value of 1 and come out ``1-SFC``,
    so any value on the SFC surface is accepted. The prose test looks for
    the surface's own name rather than the word "surface", which the
    isobaric levels the same TMP element is fetched on also carry
    ("Isobaric surface")."""
    if metadata.get("GRIB_ELEMENT", "").upper() not in (element, *_SURFACE_ELEMENT_ALIASES.get(element, ())):
        return False
    short_name = metadata.get("GRIB_SHORT_NAME", "").upper()
    searchable = " ".join(
        [
            short_name,
            metadata.get("GRIB_COMMENT", ""),
            metadata.get("GRIB_LEVEL", ""),
            description,
        ]
    ).lower()
    return short_name.endswith("-SFC") or 'sfc="' in searchable or "ground or water surface" in searchable


def _is_ecmwf_total_cloud_cover(metadata: dict[str, str], description: str) -> bool:
    """ECMWF ``tcc``: the ECMWF-local 0/6/192 on the ground surface — GDAL's
    tables do not know it, so GRIB_ELEMENT is "unknown" and the comment
    carries the raw triple, the way it does for ``tp``."""
    if metadata.get("GRIB_ELEMENT", "").lower() != "unknown":
        return False
    short_name = metadata.get("GRIB_SHORT_NAME", "").upper()
    comment = metadata.get("GRIB_COMMENT", "")
    searchable = " ".join([comment, metadata.get("GRIB_LEVEL", ""), description]).lower()
    return (short_name == "0-SFC" or "ground or water surface" in searchable) and "cat 6, subcat 192" in comment


def _is_most_unstable_cape(metadata: dict[str, str], description: str) -> bool:
    """ECMWF ``mucape``: the CAPE element departing from surface type 17,
    the level of the most unstable parcel, which GDAL describes as such and
    gives no short name of its own."""
    if metadata.get("GRIB_ELEMENT", "").upper() != "CAPE":
        return False
    searchable = " ".join([metadata.get("GRIB_COMMENT", ""), metadata.get("GRIB_LEVEL", ""), description]).lower()
    return "most unstable" in searchable or "mudl" in searchable


def _is_interval_maximum_gust(metadata: dict[str, str], description: str) -> bool:
    """ECMWF ``10fg``: the gust element on the 10 m surface (the maximum over
    the interval ending at the frame); pgrb2's instantaneous gust sits on the
    ground surface and is matched by :func:`_is_surface_record`."""
    return _is_ten_metre_wind(metadata, description, "GUST")


def _is_entire_atmosphere_record(metadata: dict[str, str], description: str, element: str) -> bool:
    """One GRIB element on the entire atmosphere (surface type 10), which
    GDAL spells ``0-EATM``; the phrase fallback catches drivers that do not.
    The fetched pgrb2 files carry the instantaneous TCDC record alone — the
    per-layer cloud covers and the interval average are never downloaded."""
    if metadata.get("GRIB_ELEMENT", "").upper() != element:
        return False
    short_name = metadata.get("GRIB_SHORT_NAME", "").upper()
    searchable = " ".join(
        [
            short_name,
            metadata.get("GRIB_COMMENT", ""),
            metadata.get("GRIB_LEVEL", ""),
            description,
        ]
    ).lower()
    return short_name == "0-EATM" or "entire atmosphere" in searchable


# The cloud layer surfaces as GDAL spells them (code table 4.5 types 214,
# 224, 234), with the phrase fallback for a driver that does not.
_CLOUD_LAYER_SURFACES: dict[str, tuple[str, tuple[str, ...]]] = {
    "LCDC": ("0-LCY", ("low cloud",)),
    "MCDC": ("0-MCY", ("middle cloud", "medium cloud")),
    "HCDC": ("0-HCY", ("high cloud",)),
}


def _is_cloud_layer_record(metadata: dict[str, str], description: str, element: str) -> bool:
    """One cloud cover element on its own layer surface. The fetched pgrb2
    files carry the instantaneous record of each layer alone — the interval
    averages are never downloaded — so element + surface is unambiguous."""
    if metadata.get("GRIB_ELEMENT", "").upper() != element:
        return False
    short_name_token, phrases = _CLOUD_LAYER_SURFACES[element]
    short_name = metadata.get("GRIB_SHORT_NAME", "").upper()
    searchable = " ".join(
        [
            short_name,
            metadata.get("GRIB_COMMENT", ""),
            metadata.get("GRIB_LEVEL", ""),
            description,
        ]
    ).lower()
    return short_name == short_name_token or any(phrase in searchable for phrase in phrases)


def _is_ten_metre_wind(metadata: dict[str, str], description: str, element: str) -> bool:
    if metadata.get("GRIB_ELEMENT", "").upper() != element:
        return False
    short_name = metadata.get("GRIB_SHORT_NAME", "").upper()
    searchable = " ".join(
        [
            short_name,
            metadata.get("GRIB_COMMENT", ""),
            metadata.get("GRIB_LEVEL", ""),
            description,
        ]
    )
    return short_name in {"10-HTGL", "10-M-HTGL"} or bool(TEN_METRE_RE.search(searchable))


def _is_mean_sea_level_pressure(metadata: dict[str, str], description: str) -> bool:
    """PRMSL on GRIB2 surface 101 (mean sea level). GDAL spells that short
    name ``0-MSL``; the phrase fallback catches drivers that do not. ECMWF
    ``msl`` is plain pressure on that surface (the registry's 0/3/0 alias),
    which GDAL names PRES, and HRRR's is the MAPS reduction MSLMA (the
    0/3/198 alias) — the surface is what makes them the same field."""
    if metadata.get("GRIB_ELEMENT", "").upper() not in {"PRMSL", "PRES", "MSLMA"}:
        return False
    short_name = metadata.get("GRIB_SHORT_NAME", "").upper()
    searchable = " ".join(
        [
            short_name,
            metadata.get("GRIB_COMMENT", ""),
            metadata.get("GRIB_LEVEL", ""),
            description,
        ]
    ).lower()
    return short_name == "0-MSL" or "mean sea level" in searchable


def _is_isobaric_record(metadata: dict[str, str], description: str, element: str, level_hpa: int) -> bool:
    """One GRIB element on one isobaric surface.

    The surface value is pascals in GRIB2 itself, and that is what GDAL
    reports: the 850 hPa record comes back as short name ``85000-ISBL`` with
    the description ``85000[Pa] ISBL="Isobaric surface"``. Hectopascals are
    accepted too — that is how the level is spelled in an ``.idx`` phrase and
    in every human-facing description — so a driver reporting ``850-ISBL``
    still matches. Whichever unit, it must name *this* level: a matcher that
    let 500 also match 1000 (or 50000 Pa also match 100000) would silently
    pick the wrong plane."""
    if metadata.get("GRIB_ELEMENT", "").upper() != element:
        return False
    short_name = metadata.get("GRIB_SHORT_NAME", "").upper()
    if short_name in {f"{level_hpa}-ISBL", f"{level_hpa * 100}-ISBL"}:
        return True
    searchable = " ".join(
        [
            metadata.get("GRIB_COMMENT", ""),
            metadata.get("GRIB_LEVEL", ""),
            description,
        ]
    )
    return bool(_isobaric_level_re(level_hpa).search(searchable))


def _isobaric_level_re(level_hpa: int) -> re.Pattern[str]:
    """The level written either way: ``850 mb`` / ``850 hPa`` / ``85000 Pa``
    (GDAL writes the pascals form with the unit bracketed, ``85000[Pa]``)."""
    return re.compile(
        rf"(?:^|[^0-9])(?:{level_hpa}\s*\[?(?:mb|hpa)\]?|{level_hpa * 100}\s*\[?pa\]?)(?:$|[^a-z0-9])",
        re.IGNORECASE,
    )


def _band_matches(variable_id: str, metadata: dict[str, str], description: str) -> bool:
    if variable_id in ("tmp2m", "dpt2m", "aptmp2m"):
        return _is_two_metre_record(metadata, description, variable_spec(variable_id).grib_element)
    if variable_id in ("prate", "prate_ave"):
        # sflux files carry only the interval-averaged PRATE record, pgrb2
        # fetches only the instantaneous one — the same surface matcher hits
        # exactly the record its source provides.
        return _is_surface_precipitation_rate(metadata, description)
    if variable_id == "tp":
        return _is_total_precipitation(metadata, description)
    if variable_id == "gust":
        return _is_surface_record(metadata, description, "GUST") or _is_interval_maximum_gust(metadata, description)
    if variable_id == "cape":
        return _is_surface_record(metadata, description, "CAPE") or _is_most_unstable_cape(metadata, description)
    if variable_id in ("dswrf", "vis", "tmpsfc", "icec", "icetk", "htsgw", "perpw", "dirpw"):
        return _is_surface_record(metadata, description, variable_spec(variable_id).grib_element)
    if variable_id == "tcdc":
        return _is_entire_atmosphere_record(metadata, description, "TCDC") or _is_ecmwf_total_cloud_cover(
            metadata, description
        )
    if variable_id == "cref":
        return _is_entire_atmosphere_record(metadata, description, variable_spec(variable_id).grib_element)
    if variable_id in ("lcdc", "mcdc", "hcdc"):
        return _is_cloud_layer_record(metadata, description, variable_spec(variable_id).grib_element)
    if variable_id in ("ugrd10m", "vgrd10m"):
        return _is_ten_metre_wind(metadata, description, variable_spec(variable_id).grib_element)
    if variable_id == "prmsl":
        return _is_mean_sea_level_pressure(metadata, description)
    isobaric = isobaric_variable(variable_id)
    if isobaric is not None:
        spec = variable_spec(variable_id)
        if not spec.grib_element:
            raise ConversionError(f"{variable_id} is derived, not a GRIB record")
        return _is_isobaric_record(metadata, description, spec.grib_element, isobaric[1])
    raise ConversionError(f"unsupported variable: {variable_id}")


def _frame_from_band(path: Path, variable_id: str, band_number: int, metadata: dict[str, str]) -> SourceFrame:
    unit_value = metadata.get("GRIB_UNIT") or metadata.get("GRIB_COMMENT", "").rsplit("[", 1)[-1].rstrip("]")
    unit = (
        normalize_unit(unit_value)
        if variable_id in SURFACE_TEMPERATURE_IDS
        else unit_value.strip().strip("[]")
    )
    raster_expression(variable_id, unit)
    run_time = _timestamp(metadata, "GRIB_REF_TIME")
    valid_time = _timestamp(metadata, "GRIB_VALID_TIME")
    forecast_seconds = metadata.get("GRIB_FORECAST_SECONDS")
    if run_time is None and valid_time is not None and forecast_seconds is not None:
        try:
            run_time = datetime.fromtimestamp(valid_time.timestamp() - int(float(forecast_seconds)), tz=UTC)
        except ValueError:
            pass
    if run_time is None or valid_time is None:
        raise ConversionError(f"missing GRIB_REF_TIME or GRIB_VALID_TIME metadata in {path}")
    delta = (valid_time - run_time).total_seconds()
    if delta < 0 or delta % 3600:
        raise ConversionError(f"forecast time is not a non-negative whole hour in {path}")
    return SourceFrame(path, band_number, variable_id, run_time, valid_time, int(delta), unit)


def inspect_grib_multi(
    path: Path,
    variable_ids: tuple[str, ...],
    *,
    optional_ids: tuple[str, ...] = (),
) -> dict[str, SourceFrame]:
    """Locate every requested variable in one gdalinfo pass over the file.

    Variables in ``optional_ids`` may be absent (they are simply omitted from
    the result); more than one match is still an error for every variable.
    """
    if not path.is_file():
        raise ConversionError(f"GRIB input does not exist: {path}")
    info = dataset_info(path, description=f"inspect {path}")
    frames: dict[str, SourceFrame] = {}
    for variable_id in variable_ids:
        spec = variable_spec(variable_id)
        matches: list[tuple[int, dict[str, str]]] = []
        for band in info.get("bands", []):
            metadata = _metadata(band)
            description = str(band.get("description", ""))
            if _band_matches(variable_id, metadata, description):
                matches.append((int(band["band"]), metadata))
        if not matches and variable_id in optional_ids:
            continue
        if len(matches) != 1:
            raise ConversionError(
                f"expected exactly one {spec.grib_element} band for {variable_id} in {path}, found {len(matches)}"
            )
        band_number, metadata = matches[0]
        frames[variable_id] = _frame_from_band(path, variable_id, band_number, metadata)
    return frames


def inspect_grib(path: Path, variable_id: str = "tmp2m") -> SourceFrame:
    return inspect_grib_multi(path, (variable_id,))[variable_id]


def discover_inputs(input_path: Path | Sequence[Path]) -> list[Path]:
    """The GRIB files a conversion reads: one file, every file in a
    directory, or exactly the files given.

    The explicit list matters when a directory holds more frames than the
    build wants — a showcase case whose forecast range shrank still has the
    longer run's frames cached beside it."""
    if not isinstance(input_path, Path):
        files = sorted(input_path)
        if not files:
            raise ConversionError("no GRIB files given")
        missing = [path for path in files if not path.is_file()]
        if missing:
            raise ConversionError(f"input does not exist: {missing[0]}")
        return files
    if input_path.is_file():
        return [input_path]
    if not input_path.is_dir():
        raise ConversionError(f"input does not exist: {input_path}")
    files = sorted(path for path in input_path.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS)
    if not files:
        raise ConversionError(f"no GRIB files found in {input_path}")
    return files
