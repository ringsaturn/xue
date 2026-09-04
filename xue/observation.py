"""Gridded observation series: a NetCDF file as a run of Xue frames.

A forecast source is a cycle on a bucket, fetched one record per forecast
hour. An observation source is the opposite shape: one local file that
already holds the whole series, one band per time, produced after the fact by
whatever decoded the original product (for the CMA radar mosaic, the
``radar-l3-mst`` tool turning published BIN tiles into NetCDF).

This module is the ingest half of that shape. It reads the file's band and
dimension metadata with one ``gdalinfo`` pass and returns the same
:class:`~xue.model.SourceFrame` list the GRIB inspectors return, so the
converter downstream (crop, quantize, temporal grouping, container write) is
the ordinary one. Two things differ from GRIB and are carried in the
returned :class:`~xue.model.PlaneSource`:

* the values are packed, so extraction runs ``gdal_translate -unscale``;
* points outside the instrument's coverage carry a fill value, and Xue has
  no bitmap — the fill becomes the bottom of the variable's codebook, which
  is the value a renderer paints as nothing.

The time axis is whatever the file carries. Observation series have gaps
(a publication missed, an outage), so the axis is *not* validated against a
published cadence the way a forecast run's is; it only has to be strictly
increasing on whole hours. Hour 0 is the first frame, which is also the
series' ``runTime``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import ConversionError
from .gdal import require_command, run_command
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
    """Everything the converter needs to read one observation file."""

    dataset: Path
    """The GDAL dataset the bands live in — for NetCDF, the
    ``NETCDF:"<file>":<variable>`` subdataset rather than the file itself."""
    frames: list[dict[str, SourceFrame]]
    """One entry per time, in axis order, keyed by variable id exactly like
    the per-file mapping the GRIB inspector produces."""
    plane_source: PlaneSource

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


def inspect_observation(path: Path, source: SourceSpec) -> ObservationSeries:
    """Read one observation file's frames, times and packing.

    The source declares exactly one input variable; the file must carry it as
    a variable of its own (a NetCDF subdataset), one band per time.
    """
    if not source.observation:
        raise ConversionError(f"{source.manifest_model} is not an observation source")
    if len(source.input_variable_ids) != 1:
        raise ConversionError(f"{source.manifest_model} must declare exactly one observation variable")
    variable_id = source.input_variable_ids[0]
    if not path.is_file():
        raise ConversionError(f"observation input does not exist: {path}")
    if path.suffix.lower() not in NETCDF_EXTENSIONS:
        raise ConversionError(f"observation input must be a NetCDF file: {path}")

    dataset = netcdf_dataset(path, variable_id)
    result = run_command(
        [require_command("gdalinfo"), "-json", str(dataset)], description=f"inspect {dataset}"
    )
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ConversionError(f"GDAL returned invalid JSON for {dataset}") from exc

    epoch, scale = _reference_time(str(info.get("metadata", {}).get("", {}).get("time#units", "")), path)
    bands = info.get("bands", [])
    if not bands:
        raise ConversionError(f"{dataset} carries no bands")

    spec = variable_spec(variable_id)
    unit = str(bands[0].get("unit", "")).strip()
    if unit != spec.output_unit:
        raise ConversionError(f"{dataset} reports unit {unit or '<missing>'}, expected {spec.output_unit}")

    times = [_band_time(band, epoch, scale, path) for band in bands]
    run_time = times[0]
    frames: list[dict[str, SourceFrame]] = []
    for band, valid_time in zip(bands, times):
        delta = (valid_time - run_time).total_seconds()
        if delta < 0 or delta % 1:
            raise ConversionError(f"{dataset} time {valid_time.isoformat()} is not a whole second after the first")
        if frames and delta <= frames[-1][variable_id].lead_seconds:
            raise ConversionError(f"{dataset} times are not strictly increasing")
        frames.append(
            {
                variable_id: SourceFrame(
                    path=dataset,
                    band=int(band["band"]),
                    variable_id=variable_id,
                    run_time=run_time,
                    valid_time=valid_time,
                    lead_seconds=int(delta),
                    unit=unit,
                )
            }
        )

    # One fill value for the whole series, in both the raw and scaled forms
    # GDAL can hand back (see PlaneSource.fill_values).
    plane_source = _plane_source(bands, spec.value_range[0], dataset)
    LOG.info(
        "%s: %d frames spanning %.1f h from %s",
        dataset,
        len(frames),
        frames[-1][variable_id].lead_seconds / 3600,
        run_time.isoformat(),
    )
    return ObservationSeries(dataset=dataset, frames=frames, plane_source=plane_source)


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
