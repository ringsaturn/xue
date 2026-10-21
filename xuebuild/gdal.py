from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import rules
from .errors import ConversionError
from .model import SourceFrame
from .rules import GdalRule
from .variables import AerosolIdentity, VariableSpec, variable_spec


SUPPORTED_EXTENSIONS = {".grb", ".grb2", ".grib2"}

def require_command(command: str) -> str:
    resolved = shutil.which(command)
    if not resolved:
        raise ConversionError(f"required command is missing: {command}")
    return resolved


def run_command(
    arguments: list[str], *, description: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one tool, its stderr becoming the error's text when it fails.
    ``env`` adds to (or overrides in) this process's environment for that
    one command — how a reader hands GDAL an HDF5 filter plugin path
    without setting it for every tool the build runs."""
    environment = {**os.environ, **env} if env else None
    try:
        return subprocess.run(arguments, check=True, text=True, capture_output=True, env=environment)
    except FileNotFoundError as exc:
        raise ConversionError(f"required command is missing: {arguments[0]}") from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        raise ConversionError(f"{description} failed: {details or f'exit status {exc.returncode}'}") from exc


def _native_gdal_info() -> Any:
    """The `xuepy` wheel's `gdal_info`, or None when this build must shell out.

    The wheel carries its own GDAL, so a build converting through the native
    encoder can inspect its inputs without a system `gdalinfo` on PATH.
    Which source a build uses follows `XUE_ENCODER`, so a run
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



def raster_expression(spec: VariableSpec, unit: str) -> str:
    """The raster expression one record of ``spec`` takes under ``unit``
    (:func:`xuebuild.rules.expression`): the unit rule's arithmetic alone.
    The header index and the gdalinfo pass compare it for identity; it is
    never written. Raises for a spelling the rule refuses."""
    return rules.expression(spec.conversion, unit)


# ---------------------------------------------------------------------------
# Band matching: one evaluator per rule of docs/registry.md §match, each
# reading the band's metadata as GDAL describes it.


def _searchable(metadata: dict[str, str], description: str, *, short_name: bool = True) -> str:
    """The band's prose, joined: the short name, comment, level and
    description, lower-cased."""
    fields = [metadata.get("GRIB_COMMENT", ""), metadata.get("GRIB_LEVEL", ""), description]
    if short_name:
        fields.insert(0, metadata.get("GRIB_SHORT_NAME", "").upper())
    return " ".join(fields).lower()


def _element(metadata: dict[str, str]) -> str:
    return metadata.get("GRIB_ELEMENT", "").upper()


def _short_name(metadata: dict[str, str]) -> str:
    return metadata.get("GRIB_SHORT_NAME", "").upper()


def _assembled(metadata: dict[str, str]) -> tuple[list[int], int]:
    """Template 4.0's assembled values and the discipline, as integers;
    empty and -1 where the driver did not assemble them."""
    try:
        values = [int(field) for field in metadata.get("GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES", "").split()]
        discipline = int(metadata.get("GRIB_DISCIPLINE", "").split("(", 1)[0])
    except ValueError:
        return [], -1
    return values, discipline


def _surface_value_matches(values: list[int], level_value: float | None) -> bool:
    """Whether template 4.0's first fixed surface (scale factor and scaled
    value at indices 10–11) spells ``level_value``; None accepts any."""
    if level_value is None:
        return True
    scale, scaled_value = values[10], values[11]
    return scaled_value != 0xFFFFFFFF and scaled_value / (10.0**scale) == level_value


@functools.lru_cache(maxsize=None)
def _height_re(metres: int) -> re.Pattern[str]:
    return re.compile(rf"(?:^|[^0-9]){metres}(?:\.0+)?\s*m(?:eter)?s?\s+above\s+ground", re.IGNORECASE)


@functools.lru_cache(maxsize=None)
def _isobaric_level_re(level_hpa: int) -> re.Pattern[str]:
    """The level written either way: ``850 mb`` / ``850 hPa`` / ``85000 Pa``
    (GDAL writes the pascals form with the unit bracketed, ``85000[Pa]``)."""
    return re.compile(
        rf"(?:^|[^0-9])(?:{level_hpa}\s*\[?(?:mb|hpa)\]?|{level_hpa * 100}\s*\[?pa\]?)(?:$|[^a-z0-9])",
        re.IGNORECASE,
    )


def _is_surface(rule: GdalRule, spec: VariableSpec, metadata: dict[str, str], description: str) -> bool:
    """One of the rule's elements on the ground or water surface. The
    fetched files carry only the instantaneous surface record of each, so
    element + surface level is unambiguous — the interval averages and the
    mixed-layer CAPE variants are never downloaded. GDAL spells the surface
    ``0-SFC``; the GFS-Wave records carry a surface value of 1 and come out
    ``1-SFC``, so any value on the SFC surface is accepted. The prose test
    looks for the surface's own name rather than the word "surface", which
    the isobaric levels the same TMP element is fetched on also carry
    ("Isobaric surface")."""
    if _element(metadata) not in rule.elements:
        return False
    searchable = _searchable(metadata, description)
    return _short_name(metadata).endswith("-SFC") or 'sfc="' in searchable or "ground or water surface" in searchable


def _is_height(rule: GdalRule, spec: VariableSpec, metadata: dict[str, str], description: str) -> bool:
    """The element on the ``metres`` m above-ground surface: GDAL spells it
    ``<metres>-HTGL``; the phrase fallback catches a driver that does not."""
    if _element(metadata) != rule.element:
        return False
    assert rule.metres is not None
    short_name = _short_name(metadata)
    searchable = " ".join([short_name, metadata.get("GRIB_COMMENT", ""), metadata.get("GRIB_LEVEL", ""), description])
    return short_name in {f"{rule.metres}-HTGL", f"{rule.metres}-M-HTGL"} or bool(_height_re(rule.metres).search(searchable))


def _is_entire_atmosphere(rule: GdalRule, spec: VariableSpec, metadata: dict[str, str], description: str) -> bool:
    """The element on the entire atmosphere (surface type 10, or NCEP's
    200, which GDAL renders like it), spelled ``0-EATM``; the phrase
    fallback catches drivers that do not. The fetched pgrb2 files carry
    the instantaneous record alone — the per-layer cloud covers and the
    interval average are never downloaded."""
    if _element(metadata) != rule.element:
        return False
    return _short_name(metadata) == "0-EATM" or "entire atmosphere" in _searchable(metadata, description)


def _is_mean_sea_level(rule: GdalRule, spec: VariableSpec, metadata: dict[str, str], description: str) -> bool:
    """One of the elements on GRIB2 surface 101 (mean sea level), which
    GDAL spells ``0-MSL``; the phrase fallback catches drivers that do
    not. The surface is what makes the elements the same field."""
    if _element(metadata) not in rule.elements:
        return False
    return _short_name(metadata) == "0-MSL" or "mean sea level" in _searchable(metadata, description)


def _is_isobaric(rule: GdalRule, spec: VariableSpec, metadata: dict[str, str], description: str) -> bool:
    """The element on one isobaric surface.

    The surface value is pascals in GRIB2 itself, and that is what GDAL
    reports: the 850 hPa record comes back as short name ``85000-ISBL`` with
    the description ``85000[Pa] ISBL="Isobaric surface"``. Hectopascals are
    accepted too — that is how the level is spelled in an ``.idx`` phrase and
    in every human-facing description — so a driver reporting ``850-ISBL``
    still matches. Whichever unit, it must name *this* level: a matcher that
    let 500 also match 1000 (or 50000 Pa also match 100000) would silently
    pick the wrong plane."""
    if _element(metadata) != rule.element:
        return False
    assert rule.hpa is not None
    if _short_name(metadata) in {f"{rule.hpa}-ISBL", f"{rule.hpa * 100}-ISBL"}:
        return True
    searchable = " ".join([metadata.get("GRIB_COMMENT", ""), metadata.get("GRIB_LEVEL", ""), description])
    return bool(_isobaric_level_re(rule.hpa).search(searchable))


def _is_cloud_layer(rule: GdalRule, spec: VariableSpec, metadata: dict[str, str], description: str) -> bool:
    """One cloud cover element on its own layer surface, as GDAL spells it
    (code table 4.5 types 214, 224, 234), with the layer's name as the
    fallback. The fetched pgrb2 files carry the instantaneous record of
    each layer alone — the interval averages are never downloaded — so
    element + surface is unambiguous. AIFS writes the layers on surfaces of
    their own (the ground, 800 hPa, 450 hPa; the registry's alternates),
    which GDAL spells ``0-80000-SFC``, ``80000-45000-ISBL`` and
    ``45000-ISBL``: those match on the layer's name in the comment ("Low
    cloud cover", "Medium cloud cover", "High cloud cover")."""
    if _element(metadata) != rule.element:
        return False
    searchable = _searchable(metadata, description)
    return _short_name(metadata) == rule.short_name.upper() or any(phrase in searchable for phrase in rule.phrases)


def _is_template(rule: GdalRule, spec: VariableSpec, metadata: dict[str, str], description: str) -> bool:
    """The record under the entry's identity or an alternate, read off the
    assembled template 4.0 values (the parameter category and number, the
    type of first fixed surface, its scale factor and scaled value at
    indices 0, 1, 9–11) beside the discipline: the element and short name
    do not say which soil layer a record is in a form two centres share —
    GDAL spells GFS's ``0-0.1-DBLL`` and names the element TSOIL / SOILW,
    and ECMWF's ``0-1-`` on a soil level with SOILTMP / VSOILM — while the
    template does. A record of another template (an interval statistic)
    is not it."""
    if metadata.get("GRIB_PDS_PDTN") != "0":
        return False
    values, discipline = _assembled(metadata)
    if len(values) < 12:
        return False
    identities = [
        (spec.grib2_discipline, spec.grib2_category, spec.grib2_number, spec.grib2_level_type, spec.grib2_level_value),
        *(
            (alternate.discipline, alternate.category, alternate.number, alternate.level_type, alternate.level_value)
            for alternate in spec.grib2_alternates
        ),
    ]
    for identity_discipline, category, number, level_type, level_value in identities:
        if (discipline, values[0], values[1], values[9]) != (identity_discipline, category, number, level_type):
            continue
        if _surface_value_matches(values, level_value):
            return True
    return False


def _is_template_aerosol(rule: GdalRule, spec: VariableSpec, metadata: dict[str, str], description: str) -> bool:
    """One GRIB2 aerosol product (template 4.48) under the entry's
    parameter *and* aerosol identity. The driver's tables name the fields
    by element (``AOTK``, ``PMTF``) and surface, which a dozen of
    GEFS-Aerosols' records share; what tells them apart is in the template
    itself, which GDAL exposes whole as
    ``GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES`` (the template's fields in order,
    space-separated) beside ``GRIB_PDS_PDTN``. A record of another
    template, or one the driver did not assemble, is not this record."""
    identity = spec.grib2_aerosol
    if identity is None or metadata.get("GRIB_PDS_PDTN") != "48":
        return False
    values, discipline = _assembled(metadata)
    if len(values) < 23:
        return False
    if (discipline, values[0], values[1]) != (spec.grib2_discipline, spec.grib2_category, spec.grib2_number):
        return False
    if values[20] != spec.grib2_level_type:
        return False
    if spec.grib2_level_value is not None:
        scale, scaled_value = values[21], values[22]
        if scaled_value == 0xFFFFFFFF or scaled_value / (10.0**scale) != spec.grib2_level_value:
            return False
    return _assembled_aerosol(values) == identity


def _assembled_aerosol(values: list[int]) -> AerosolIdentity:
    """The aerosol identity of template 4.48's assembled values: type at
    index 2, the size interval at 3–7, the wavelength interval at 8–12. An
    interval of the missing type carries no limits, as the header index
    reads it (:mod:`xuebuild.grib2`)."""
    missing = AerosolIdentity.MISSING_TYPE

    def limit(index: int) -> tuple[int, int] | None:
        scale, scaled_value = values[index], values[index + 1]
        return None if scaled_value == 0xFFFFFFFF else (scale, scaled_value)

    size_type, wavelength_type = values[3], values[8]
    return AerosolIdentity(
        values[2],
        size_type=size_type,
        size_first=None if size_type == missing else limit(4),
        size_second=None if size_type == missing else limit(6),
        wavelength_type=wavelength_type,
        wavelength_first=None if wavelength_type == missing else limit(9),
        wavelength_second=None if wavelength_type == missing else limit(11),
    )


def _is_template_rank(rule: GdalRule, spec: VariableSpec, metadata: dict[str, str], description: str) -> bool:
    """One rank of an ordered sequence (code table 4.5 type 241, local to
    NCEP): a GFS-Wave swell partition, under the entry's parameter and
    rank. The three partitions share element, parameter and grid and differ
    in the surface value alone, which GDAL, knowing no name for the surface,
    spells ``1-RESERVED(241)`` — so the rank is read off the assembled
    template 4.0 values beside the discipline and parameter, the way a soil
    layer is. Where the driver did not assemble the template, the short
    name is the fallback, rank included."""
    if _element(metadata) != spec.grib_element:
        return False
    rank = spec.grib2_level_value
    values, discipline = _assembled(metadata)
    if metadata.get("GRIB_PDS_PDTN") == "0" and len(values) >= 12:
        if (discipline, values[0], values[1], values[9]) != (
            spec.grib2_discipline,
            spec.grib2_category,
            spec.grib2_number,
            spec.grib2_level_type,
        ):
            return False
        return rank is not None and _surface_value_matches(values, rank)
    return _short_name(metadata) == f"{rank:g}-RESERVED({spec.grib2_level_type})"


def _is_template_mrms(rule: GdalRule, spec: VariableSpec, metadata: dict[str, str], description: str) -> bool:
    """One MRMS product under the entry's MRMS identity — its own (the
    reflectivity volume's levels) or its discipline-209 alternate (the
    composite, the rate): GDAL names the record by the product (its local
    table for centre 161 — ``MergedReflectivityQCComposite``,
    ``PrecipRate``, ``MergedReflectivityQC``), on the MRMS-local discipline
    209, at a height above mean sea level. The element is not enough: the
    volume's 33 levels share one, so the level must be this identity's too
    — read off the assembled template (type of first fixed surface, scale
    factor and scaled value at indices 9–11 of template 4.0), or, where the
    driver did not assemble it, off the short name GDAL spells it with
    (``3000-GPML``)."""
    if not spec.mrms_product:
        return False
    element = spec.mrms_product.split("_", 1)[0]
    if metadata.get("GRIB_ELEMENT", "") != element or metadata.get("GRIB_DISCIPLINE", "") != "209":
        return False
    category, number, level_type, level_value = next(
        (
            (alternate.category, alternate.number, alternate.level_type, alternate.level_value)
            for alternate in spec.grib2_alternates
            if alternate.discipline == 209
        ),
        (spec.grib2_category, spec.grib2_number, spec.grib2_level_type, spec.grib2_level_value),
    )
    values, _discipline = _assembled(metadata)
    if metadata.get("GRIB_PDS_PDTN") == "0" and len(values) >= 12:
        if (values[0], values[1], values[9]) != (category, number, level_type):
            return False
        return _surface_value_matches(values, level_value)
    if level_type != 102 or level_value is None:
        return True
    return _short_name(metadata) == f"{level_value:g}-GPML"


def _is_total_precipitation(rule: GdalRule, spec: VariableSpec, metadata: dict[str, str], description: str) -> bool:
    """ECMWF open data tp: GRIB2 discipline 0, category 1, local parameter
    193 — GDAL's tables do not know it, so GRIB_ELEMENT is "unknown" and the
    comment carries the raw triple. AIFS writes its run total under the WMO
    0/1/52, which GDAL's tables name TPRATE ("Total precipitation rate");
    the fetched files carry no other record of that element."""
    if metadata.get("GRIB_ELEMENT", "").lower() not in {"unknown", "tp", "apcp", "tprate"}:
        return False
    comment = metadata.get("GRIB_COMMENT", "")
    searchable = _searchable(metadata, description, short_name=False)
    return (_short_name(metadata) == "0-SFC" or "surface" in searchable) and (
        "cat 1, subcat 193" in comment or "total precipitation" in searchable
    )


def _is_local_parameter(rule: GdalRule, spec: VariableSpec, metadata: dict[str, str], description: str) -> bool:
    """A centre-local parameter on the ground surface GDAL's tables do not
    know (ECMWF ``tcc``, the ECMWF-local 0/6/192): GRIB_ELEMENT is
    "unknown" and the comment carries the raw triple, the way it does for
    ``tp``."""
    if metadata.get("GRIB_ELEMENT", "").lower() != "unknown":
        return False
    comment = metadata.get("GRIB_COMMENT", "")
    searchable = _searchable(metadata, description, short_name=False)
    return (_short_name(metadata) == "0-SFC" or "ground or water surface" in searchable) and (
        f"cat {rule.category}, subcat {rule.number}" in comment
    )


def _is_phrase(rule: GdalRule, spec: VariableSpec, metadata: dict[str, str], description: str) -> bool:
    """The element described by one of the phrases: ECMWF ``mucape``, the
    CAPE element departing from surface type 17, the level of the most
    unstable parcel, which GDAL describes as such and gives no short name
    of its own."""
    if _element(metadata) != rule.element:
        return False
    searchable = _searchable(metadata, description, short_name=False)
    return any(phrase in searchable for phrase in rule.phrases)


def _is_geopotential_surface(rule: GdalRule, spec: VariableSpec, metadata: dict[str, str], description: str) -> bool:
    """The geopotential on the ground surface: ECMWF open data's orography,
    0/3/4, which GDAL names ``GP`` and describes only in the comment."""
    if _element(metadata) not in rule.elements:
        return False
    searchable = _searchable(metadata, description)
    return (
        _short_name(metadata).endswith("-SFC") or "ground or water surface" in searchable
    ) and "geopotential" in searchable


_MATCHERS: dict[str, Callable[[GdalRule, VariableSpec, dict[str, str], str], bool]] = {
    "surface": _is_surface,
    "height": _is_height,
    "entire-atmosphere": _is_entire_atmosphere,
    "mean-sea-level": _is_mean_sea_level,
    "isobaric": _is_isobaric,
    "cloud-layer": _is_cloud_layer,
    "template": _is_template,
    "template-aerosol": _is_template_aerosol,
    "template-rank": _is_template_rank,
    "template-mrms": _is_template_mrms,
    "total-precipitation": _is_total_precipitation,
    "local-parameter": _is_local_parameter,
    "phrase": _is_phrase,
    "geopotential-surface": _is_geopotential_surface,
}
assert set(_MATCHERS) == rules.MATCH_RULES


def rule_matches(rule: GdalRule, spec: VariableSpec, metadata: dict[str, str], description: str) -> bool:
    """Whether one rule accepts the band."""
    return _MATCHERS[rule.rule](rule, spec, metadata, description)


def _band_matches(variable_id: str, metadata: dict[str, str], description: str) -> bool:
    """Whether the band is the variable's record: any of the entry's GDAL
    rules accepts it. A field no GRIB band carries has no rules and
    matches nothing."""
    spec = variable_spec(variable_id)
    return any(rule_matches(rule, spec, metadata, description) for rule in spec.gdal_rules or ())


def _frame_from_band(path: Path, variable_id: str, band_number: int, metadata: dict[str, str]) -> SourceFrame:
    spec = variable_spec(variable_id)
    unit_value = metadata.get("GRIB_UNIT") or metadata.get("GRIB_COMMENT", "").rsplit("[", 1)[-1].rstrip("]")
    # A temperature's unit is carried as the one letter its rule reads it
    # through; any other as GDAL spells it, brackets off.
    unit = (
        rules.temperature_unit(unit_value)
        if spec.conversion.rule in ("celsius", "soil-temperature")
        else unit_value.strip().strip("[]")
    )
    raster_expression(spec, unit)
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
