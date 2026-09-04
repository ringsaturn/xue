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
from .variables import variable_spec


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


def celsius_expression(unit: str) -> str:
    normalized = normalize_unit(unit)
    if normalized == "K":
        value = "A-273.15"
    elif normalized == "F":
        value = "(A-32)*5/9"
    else:
        value = "A"
    return f"maximum(-60,minimum(50,{value}))"


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


def accumulation_expression(unit: str) -> str:
    """ECMWF tp arrives in metres of accumulated water; GDAL reports the
    ECMWF-local parameter's unit as "-". The expression is identity — the
    converter de-accumulates consecutive planes itself."""
    compact = unit.strip().strip("[]()").lower()
    if compact not in {"-", "m", ""}:
        raise ConversionError(f"unsupported precipitation accumulation unit: {unit or '<missing>'}")
    return "A"


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
    raise ConversionError(f"unsupported variable: {variable_id}")


def _is_two_metre_temperature(metadata: dict[str, str], description: str) -> bool:
    if metadata.get("GRIB_ELEMENT", "").upper() != "TMP":
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


def _is_surface_flux(metadata: dict[str, str], description: str, element: str) -> bool:
    """Surface radiative flux (e.g. DSWRF). The fetched sflux files carry only
    the instantaneous record, so element + surface level is unambiguous."""
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
    return short_name == "0-SFC" or "surface" in searchable


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


def _band_matches(variable_id: str, metadata: dict[str, str], description: str) -> bool:
    if variable_id == "tmp2m":
        return _is_two_metre_temperature(metadata, description)
    if variable_id in ("prate", "prate_ave"):
        # sflux files carry only the interval-averaged PRATE record, pgrb2
        # fetches only the instantaneous one — the same surface matcher hits
        # exactly the record its source provides.
        return _is_surface_precipitation_rate(metadata, description)
    if variable_id == "tp":
        return _is_total_precipitation(metadata, description)
    if variable_id == "dswrf":
        return _is_surface_flux(metadata, description, variable_spec(variable_id).grib_element)
    if variable_id in ("ugrd10m", "vgrd10m"):
        return _is_ten_metre_wind(metadata, description, variable_spec(variable_id).grib_element)
    raise ConversionError(f"unsupported variable: {variable_id}")


def _frame_from_band(path: Path, variable_id: str, band_number: int, metadata: dict[str, str]) -> SourceFrame:
    unit_value = metadata.get("GRIB_UNIT") or metadata.get("GRIB_COMMENT", "").rsplit("[", 1)[-1].rstrip("]")
    unit = normalize_unit(unit_value) if variable_id == "tmp2m" else unit_value.strip().strip("[]")
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
    result = run_command([require_command("gdalinfo"), "-json", str(path)], description=f"inspect {path}")
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ConversionError(f"GDAL returned invalid JSON for {path}") from exc
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
