"""Gridded observation series: a NetCDF file as a run of Xue frames.

A forecast source is a cycle on a bucket, fetched one record per forecast
hour. An observation source is the opposite shape: one local file that
already holds the whole series, one band per time, produced after the fact by
whatever decoded the original product (for the CMA radar mosaic, the
archive of decoded portal tiles, read back as NetCDF).

This module is the ingest half of that shape. It reads each variable's band
and dimension metadata with one ``gdalinfo`` pass and returns the same
:class:`~xue.model.SourceFrame` list the GRIB inspectors return, so the
converter downstream (crop, quantize, temporal grouping, container write) is
the ordinary one. A window may carry several variables — a satellite
window is one series file per channel and per produced component
(:func:`series_files`) — each read with its own packing. Two things differ
from GRIB and are carried in the returned per-variable
:class:`~xue.model.PlaneSource`:

* the values are packed, so extraction runs ``gdal_translate -unscale``;
* points outside the instrument's coverage carry a fill value, and Xue has
  no bitmap — the fill becomes the bottom of the variable's codebook, which
  is the value a renderer paints as nothing.

The time axis is whatever the file carries. Observation series have gaps
(a publication missed, an outage), so the axis is *not* validated against a
published cadence the way a forecast run's is; it only has to be strictly
increasing on whole seconds. For a local archive file (the CMA mosaic) the
first frame is the series' ``runTime``. For a fetched window
(:attr:`~xuebuild.sources.SourceSpec.cadence_seconds` set: the JMA nowcast)
the window is the axis, the same rule the MRMS frames follow
(``binconvert._snap_observation_frames``): each time is snapped down to its
cadence slot, the run time is the whole hour the first slot falls in — the
hour the run id names — and a frame's offset is its slot's distance from it.

**A forecast series** (``series_file`` on a source that is not an
observation: the ECMWF IFS HRES run ``om2nc`` resamples off the Open-Meteo
bucket, :mod:`xuebuild.om2nccli`) is read here too, and differs in three
ways. Its run time is not its first frame but the epoch of the ``time``
coordinate — ``hours since <cycle>`` — which is the cycle itself, so a
variable whose series starts at the first step still carries the lead times
of that cycle; the file's own ``forecast_reference_time`` must agree with
it. Its variables need not all cover the axis: one the source lists under
:attr:`~xuebuild.sources.SourceSpec.optional_at_analysis` (an interval
total, a mean, a maximum — none of which exists at the analysis) may lack
exactly the lead-zero frame, and each returned frame mapping then holds
only the variables that time has. And the file may spell a unit another way
than the registry does (:func:`accepted_series_units`). Everything
downstream — the crop, the quantization, the temporal grouping — is the
ordinary path, and the forecast axis itself is validated against the
source's published steps by the converter, as any cycle's is.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import ConversionError
from .gdal import dataset_info
from .model import PlaneSource, SourceFrame
from .sources import SourceSpec
from .variables import SURFACE_TEMPERATURE_IDS, VariableSpec, isobaric_variable, variable_spec

LOG = logging.getLogger(__name__)

NETCDF_EXTENSIONS = {".nc", ".nc4", ".cdf"}

# "<unit> since <ISO timestamp>", the CF convention for a time coordinate.
_TIME_UNITS_RE = re.compile(r"^\s*(seconds|minutes|hours|days)\s+since\s+(.+?)\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}


@dataclass(frozen=True)
class ObservationSeries:
    """Everything the converter needs to read one observation window."""

    datasets: dict[str, Path]
    """The GDAL dataset each variable's bands live in, by variable id — for
    NetCDF, the ``NETCDF:"<file>":<variable>`` subdataset rather than the
    file itself. One file per variable (a satellite window, one series
    per channel or produced component), or one file carrying several
    subdatasets (the radar mosaics' single variable)."""
    frames: list[dict[str, SourceFrame]]
    """One entry per time, in axis order, keyed by variable id exactly like
    the per-file mapping the GRIB inspector produces. Every variable of a
    window is in every entry, since every series of a window carries the
    same axis; on a forecast series an analysis-optional variable is absent
    from the lead-zero entry, exactly as its record is absent from an f000
    GRIB."""
    plane_sources: dict[str, PlaneSource]
    """How each variable's bands are read: its own packing and fill."""
    producers: dict[str, tuple[str, str]]
    """For a produced variable, the ``(id, version)`` its series was
    stamped with — the ``producer`` block the bundle metadata carries."""

    @property
    def dataset(self) -> Path:
        """The first variable's dataset: the grid every series shares."""
        return next(iter(self.datasets.values()))

    @property
    def plane_source(self) -> PlaneSource:
        """The first variable's plane source — the one there is, for the
        one-variable series the radar sources write."""
        return next(iter(self.plane_sources.values()))

    @property
    def lead_seconds(self) -> list[int]:
        """Each frame's offset from the first, in seconds."""
        return [next(iter(frames.values())).lead_seconds for frames in self.frames]


def netcdf_dataset(path: Path, variable: str) -> Path:
    """The GDAL connection string for one variable of a NetCDF file. It is
    not a filesystem path; it is carried as one because that is what every
    downstream ``gdal_translate`` call takes."""
    return Path(f'NETCDF:"{path}":{variable}')


def _reference_time(units: str, source: Path) -> tuple[datetime, int]:
    match = _TIME_UNITS_RE.match(units)
    if not match:
        raise ConversionError(f"unsupported time units {units!r} in {source}")
    scale = _UNIT_SECONDS[match.group(1).lower()]
    text = match.group(2).replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        epoch = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConversionError(f"unsupported time epoch {match.group(2)!r} in {source}") from exc
    return (epoch if epoch.tzinfo else epoch.replace(tzinfo=UTC)).astimezone(UTC), scale


def _band_time(band: dict[str, Any], epoch: datetime, scale: int, source: Path) -> datetime:
    metadata = band.get("metadata", {}).get("", {})
    raw = metadata.get("NETCDF_DIM_time")
    if raw is None:
        raise ConversionError(f"band {band.get('band')} of {source} carries no time coordinate")
    try:
        offset = float(raw)
    except ValueError as exc:
        raise ConversionError(f"band {band.get('band')} of {source} has an invalid time coordinate") from exc
    return epoch + timedelta(seconds=offset * scale)


def series_variable_name(variable_id: str) -> str:
    """The NetCDF variable one series is read from. Every tool this pipeline
    drives names the variable by its Xue id, except ``om2nc``, which keeps
    Open-Meteo's own name inside the file while the file beside it is named
    by the Xue id (:attr:`~xuebuild.variables.VariableSpec.open_meteo`).
    Mirrored by ``encode/observation.rs``."""
    return variable_spec(variable_id).open_meteo or variable_id


#: Units one file may spell another way than the registry does, by the
#: registry's own spelling. udunits writes a product with a space and an
#: exponent with a minus sign, so ``m s-1`` is ``m/s`` and ``W m-2`` is
#: ``W/m²`` — the same unit, and no conversion follows from accepting it.
_UNIT_SPELLINGS: dict[str, tuple[str, ...]] = {
    "m/s": ("m s-1",),
    "W/m²": ("W m-2",),
    "J/kg": ("J kg-1",),
    "mm": ("kg m-2",),
    "°C": ("degC",),
}


def accepted_series_units(spec: VariableSpec) -> tuple[str, ...]:
    """Every unit a series file may report for one variable.

    A GRIB record is matched on its identity and its unit is checked against
    the registry's; a series file has no identity to match, so the unit is
    the whole of the check and it must be strict — a number quantized in the
    wrong unit is silent. The rule is: the registry's ``output_unit``, the
    same unit spelled the way udunits spells it (:data:`_UNIT_SPELLINGS`),
    or an input unit the converter already knows how to turn into it for
    *this* variable (``binconvert._convert_units``) — kelvin or fahrenheit
    for a temperature, pascals for the sea level pressure, metres for the
    visibility. Anything else is refused. Mirrored by
    ``encode/observation.rs``, which must answer the same list."""
    units = [spec.output_unit, *_UNIT_SPELLINGS.get(spec.output_unit, ())]
    isobaric = isobaric_variable(spec.id)
    if spec.id in SURFACE_TEMPERATURE_IDS or (isobaric is not None and isobaric[0] == "tmp"):
        # The three spellings normalize_unit accepts, which is what the
        # converter reads a temperature through.
        units += ["K", "C", "F"]
    elif spec.id == "prmsl":
        units.append("Pa")
    elif spec.id == "vis":
        units.append("m")
    return tuple(dict.fromkeys(units))


def series_files(path: Path, variable_ids: tuple[str, ...]) -> dict[str, Path]:
    """Which NetCDF file each variable of an observation window is in.

    A file is every variable's (the mosaics: one file, one variable, and a
    second variable would be another subdataset of it). A directory is
    read two ways: one file per variable named ``<stem>.<variable>.nc``
    (a satellite window, :mod:`xuebuild.satellite.assemble`), else the
    one NetCDF file it holds for every variable. Mirrored by
    ``encode/observation.rs``."""
    if path.is_file():
        return dict.fromkeys(variable_ids, path)
    if not path.is_dir():
        raise ConversionError(f"observation input does not exist: {path}")
    files = sorted(item for item in path.iterdir() if item.is_file() and item.suffix.lower() in NETCDF_EXTENSIONS)
    per_variable = {
        variable_id: [item for item in files if item.name.endswith(f".{variable_id}{item.suffix}")]
        for variable_id in variable_ids
    }
    if any(per_variable.values()):
        resolved: dict[str, Path] = {}
        for variable_id, candidates in per_variable.items():
            if len(candidates) != 1:
                raise ConversionError(
                    f"{path} must hold exactly one series file for {variable_id} (<stem>.{variable_id}.nc), "
                    f"it holds {len(candidates)}"
                )
            resolved[variable_id] = candidates[0]
        return resolved
    if len(files) != 1:
        raise ConversionError(f"a run directory holds exactly one NetCDF series, {path} holds {len(files)}")
    return dict.fromkeys(variable_ids, files[0])


def inspect_observation(
    path: Path, source: SourceSpec, variable_ids: tuple[str, ...] | None = None
) -> ObservationSeries:
    """Read one observation window's frames, times, packing and producers.

    ``path`` is one NetCDF file or a run directory (:func:`series_files`).
    ``variable_ids`` are the variables to read, the source's inputs by
    default; each must be a NetCDF variable (subdataset) of its file, one
    band per time, and every variable must carry the same time axis — bar an
    analysis-optional one on a forecast series, which may lack the lead-zero
    frame. A variable the registry marks as produced must be stamped with
    that producer's id, and the version beside it is returned.
    """
    if not source.observation and not source.series_file:
        raise ConversionError(f"{source.manifest_model} is not read from a NetCDF series")
    if variable_ids is None:
        variable_ids = source.input_variable_ids
    if not variable_ids:
        raise ConversionError(f"{source.manifest_model} declares no observation variable")
    files = series_files(path, variable_ids)

    datasets: dict[str, Path] = {}
    plane_sources: dict[str, PlaneSource] = {}
    producers: dict[str, tuple[str, str]] = {}
    per_variable_frames: dict[str, list[SourceFrame]] = {}
    for variable_id in variable_ids:
        file = files[variable_id]
        if file.suffix.lower() not in NETCDF_EXTENSIONS:
            raise ConversionError(f"observation input must be a NetCDF file: {file}")
        dataset = netcdf_dataset(file, series_variable_name(variable_id))
        info = dataset_info(dataset, description=f"inspect {dataset}")

        attributes_global = info.get("metadata", {}).get("", {})
        epoch, scale = _reference_time(str(attributes_global.get("time#units", "")), file)
        bands = info.get("bands", [])
        if not bands:
            raise ConversionError(f"{dataset} carries no bands")

        spec = variable_spec(variable_id)
        unit = str(bands[0].get("unit", "")).strip()
        accepted = accepted_series_units(spec)
        if unit not in accepted:
            raise ConversionError(
                f"{dataset} reports unit {unit or '<missing>'}, expected {' or '.join(accepted)}"
            )
        # A produced variable carries the producer that made it as two
        # attributes of the variable (the fetch stage stamps them,
        # xuebuild/satellite/assemble.py); the id must be the registry's,
        # the version is whatever ran and goes into the metadata.
        attributes = bands[0].get("metadata", {}).get("", {})
        producer_id = attributes.get("producer_id")
        producer_version = attributes.get("producer_version")
        if spec.producer_id is not None:
            if producer_id != spec.producer_id:
                raise ConversionError(
                    f"{dataset} was produced by {producer_id or '<nothing>'}, not {spec.producer_id}"
                )
            if not producer_version or not str(producer_version).strip():
                raise ConversionError(f"{dataset} carries no producer_version")
            producers[variable_id] = (str(producer_id), str(producer_version).strip())
        elif producer_id is not None:
            raise ConversionError(f"{dataset} is stamped by producer {producer_id!r}, but {variable_id} is not a produced variable")

        times = [_band_time(band, epoch, scale, file) for band in bands]
        if not source.observation:
            # A forecast series names its cycle in the time coordinate's own
            # epoch ("hours since <cycle>"), so the run is that epoch and
            # every band is a lead time from it — a variable whose series
            # starts at the first step keeps the cycle's numbering. The
            # file's own reference time must say the same thing.
            run_time = epoch
            _check_reference_time(attributes_global, epoch, dataset)
        elif source.cadence_seconds:
            cadence = source.cadence_seconds
            times = [datetime.fromtimestamp(int(time.timestamp()) // cadence * cadence, tz=UTC) for time in times]
            run_time = times[0].replace(minute=0, second=0, microsecond=0)
        else:
            run_time = times[0]
        frames: list[SourceFrame] = []
        for band, valid_time in zip(bands, times):
            delta = (valid_time - run_time).total_seconds()
            if delta < 0 or delta % 1:
                raise ConversionError(f"{dataset} time {valid_time.isoformat()} is not a whole second after the first")
            if frames and delta <= frames[-1].lead_seconds:
                raise ConversionError(f"{dataset} times are not strictly increasing")
            frames.append(
                SourceFrame(
                    path=dataset,
                    band=int(band["band"]),
                    variable_id=variable_id,
                    run_time=run_time,
                    valid_time=valid_time,
                    lead_seconds=int(delta),
                    unit=unit,
                )
            )
        datasets[variable_id] = dataset
        # One fill value for the whole series, in both the raw and scaled
        # forms GDAL can hand back (see PlaneSource.fill_values).
        plane_sources[variable_id] = _plane_source(bands, spec.value_range[0], dataset)
        per_variable_frames[variable_id] = frames

    # The run's axis is the axis of a variable that carries every frame; a
    # variable the source lists as optional at the analysis may be short of
    # exactly the lead-zero frame and must match the rest one for one. (Only
    # a forecast series has such a variable; a window's series all carry the
    # same times.)
    first_id = next(
        (variable_id for variable_id in variable_ids if variable_id not in source.optional_at_analysis),
        variable_ids[0],
    )
    axis = [(frame.run_time, frame.lead_seconds) for frame in per_variable_frames[first_id]]
    analysis_less = axis[1:] if axis and axis[0][1] == 0 else None
    for variable_id in variable_ids:
        other = [(frame.run_time, frame.lead_seconds) for frame in per_variable_frames[variable_id]]
        if other == axis:
            continue
        if variable_id in source.optional_at_analysis and other == analysis_less:
            continue
        raise ConversionError(f"{datasets[variable_id]} carries another time axis than {datasets[first_id]}")
    by_key = {
        variable_id: {(frame.run_time, frame.lead_seconds): frame for frame in frames}
        for variable_id, frames in per_variable_frames.items()
    }
    frames_by_time = [
        {variable_id: by_key[variable_id][key] for variable_id in variable_ids if key in by_key[variable_id]}
        for key in axis
    ]
    LOG.info(
        "%s: %d frames of %s spanning %.1f h from %s",
        path,
        len(frames_by_time),
        ", ".join(variable_ids),
        axis[-1][1] / 3600,
        axis[0][0].isoformat(),
    )
    return ObservationSeries(datasets=datasets, frames=frames_by_time, plane_sources=plane_sources, producers=producers)


def _declares_nan_fill(bands: list[dict[str, Any]]) -> bool:
    """Whether the bands declare a CF ``_FillValue`` of NaN in their own
    attributes. Mirrored by ``encode/observation.rs``."""
    raw = bands[0].get("metadata", {}).get("", {}).get("_FillValue")
    if raw is None:
        return False
    try:
        return math.isnan(float(raw))
    except ValueError:
        return False


def _check_reference_time(attributes: dict[str, Any], epoch: datetime, dataset: Path) -> None:
    """Hold a forecast series' ``forecast_reference_time`` to the epoch of
    its time coordinate. Both name the cycle, and the run is read from the
    epoch, so a file where they disagree is one this converter has
    misunderstood rather than one to build from. A file that carries no such
    attribute passes: it is CF's business, not the format's."""
    raw = attributes.get("NC_GLOBAL#forecast_reference_time")
    if raw is None:
        return
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        reference = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConversionError(f"{dataset} has an unreadable forecast_reference_time {raw!r}") from exc
    reference = (reference if reference.tzinfo else reference.replace(tzinfo=UTC)).astimezone(UTC)
    if reference != epoch:
        raise ConversionError(
            f"{dataset} was made for {reference.isoformat()} but its time axis counts from {epoch.isoformat()}"
        )


def _fill_key(nodata: Any) -> float | str | None:
    """One band's reported nodata as a set key: None as itself, a NaN as
    the text ``"nan"`` and any other value as a float."""
    if nodata is None:
        return None
    number = float(nodata)
    return "nan" if math.isnan(number) else number


def _plane_source(bands: list[dict[str, Any]], fill_replacement: float, dataset: Path) -> PlaneSource:
    scales = {float(band.get("scale", 1.0) or 1.0) for band in bands}
    offsets = {float(band.get("offset", 0.0) or 0.0) for band in bands}
    # A NaN nodata equals nothing, itself included, so the bands' fills are
    # collapsed by a key that spells every NaN the same way — whichever of
    # the text "NaN" or a real float a gdalinfo source hands back — as the
    # Rust port compares them (``encode/observation.rs``).
    fills = {_fill_key(band.get("noDataValue")) for band in bands}
    if len(scales) != 1 or len(offsets) != 1 or len(fills) != 1:
        raise ConversionError(f"{dataset} bands disagree on packing or fill value")
    scale, offset, fill = scales.pop(), offsets.pop(), fills.pop()
    if fill is None or fill == "nan":
        # A CF ``_FillValue`` of NaN — every om2nc series carries one, and
        # the land under sea ice thickness is written with it. Nothing equals
        # a NaN, so it can never be one of the fill *values*; it is matched
        # as a NaN and becomes the same codebook bottom. It is read off the
        # band's own attributes as well as off the reported nodata, because
        # a NaN is not a JSON number and the two ``gdalinfo`` sources this
        # pipeline reads (the subprocess, the wheel's linked GDAL) carry it
        # differently — one as the text "NaN", the other not at all.
        if fill is not None or _declares_nan_fill(bands):
            return PlaneSource(unscale=True, fill_nan=True, fill_replacement=fill_replacement)
        return PlaneSource(unscale=True, fill_replacement=fill_replacement)
    return PlaneSource(
        unscale=True,
        fill_values=(float(fill), float(fill) * scale + offset),
        fill_replacement=fill_replacement,
    )
