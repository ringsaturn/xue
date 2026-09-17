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
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import ConversionError
from .gdal import dataset_info
from .model import PlaneSource, SourceFrame
from .sources import SourceSpec
from .variables import variable_spec

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
    the per-file mapping the GRIB inspector produces; every variable is in
    every entry, since every series of a window carries the same axis."""
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
    band per time, and every variable must carry the same time axis. A
    variable the registry marks as produced must be stamped with that
    producer's id, and the version beside it is returned.
    """
    if not source.observation:
        raise ConversionError(f"{source.manifest_model} is not an observation source")
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
        dataset = netcdf_dataset(file, variable_id)
        info = dataset_info(dataset, description=f"inspect {dataset}")

        epoch, scale = _reference_time(str(info.get("metadata", {}).get("", {}).get("time#units", "")), file)
        bands = info.get("bands", [])
        if not bands:
            raise ConversionError(f"{dataset} carries no bands")

        spec = variable_spec(variable_id)
        unit = str(bands[0].get("unit", "")).strip()
        if unit != spec.output_unit:
            raise ConversionError(f"{dataset} reports unit {unit or '<missing>'}, expected {spec.output_unit}")
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
        if source.cadence_seconds:
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

    first_id = variable_ids[0]
    axis = [(frame.run_time, frame.lead_seconds) for frame in per_variable_frames[first_id]]
    for variable_id in variable_ids[1:]:
        other = [(frame.run_time, frame.lead_seconds) for frame in per_variable_frames[variable_id]]
        if other != axis:
            raise ConversionError(f"{datasets[variable_id]} carries another time axis than {datasets[first_id]}")
    frames_by_time = [
        {variable_id: per_variable_frames[variable_id][index] for variable_id in variable_ids} for index in range(len(axis))
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


def _plane_source(bands: list[dict[str, Any]], fill_replacement: float, dataset: Path) -> PlaneSource:
    scales = {float(band.get("scale", 1.0) or 1.0) for band in bands}
    offsets = {float(band.get("offset", 0.0) or 0.0) for band in bands}
    fills = {band.get("noDataValue") for band in bands}
    if len(scales) != 1 or len(offsets) != 1 or len(fills) != 1:
        raise ConversionError(f"{dataset} bands disagree on packing or fill value")
    scale, offset, fill = scales.pop(), offsets.pop(), fills.pop()
    if fill is None:
        return PlaneSource(unscale=True, fill_replacement=fill_replacement)
    return PlaneSource(
        unscale=True,
        fill_values=(float(fill), float(fill) * scale + offset),
        fill_replacement=fill_replacement,
    )
