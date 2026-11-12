"""The Hong Kong Observatory's gridded rainfall nowcast, fetched into the
series file both encoders read (``docs/contribution/sources.md``,
"hkonowcast").

The Observatory publishes its nowcast on DATA.GOV.HK as one CSV,
``Gridded_rainfall_nowcast.csv``, replaced every twelve minutes or so: the
latest issue only, no listing and no archive. One file is one issue — the
``Updated Date and Time`` column, Hong Kong time (UTC+8) — and four
half-hourly accumulations ending 30, 60, 90 and 120 minutes after it, in
millimetres to two decimals, on 121 × 121 points over the Pearl River
Delta. Rows run accumulation by accumulation, north to south, west to
east.

The points are not a latitude/longitude grid. The longitudes are evenly
spaced (0.01946°), but the latitude spacing shrinks northwards from 0.0181°
to 0.0178°: the rows are evenly spaced in spherical Mercator, at the
longitude step (to the three decimals the file prints). A regular
latitude axis through the end rows misses the middle rows by up to 0.004°,
a quarter of a row. Both encoders read only regular latitude/longitude
series, and the series-file path leaves every projection to the fetch, so
the fetch samples the points onto a regular grid of whole thousandths —
0.019° by 0.018°, the steps ``grid.snap_regional_steps`` recognises, which
the reference and the native encoder then read to the same double — by
nearest point along each axis separately (Mercator separates: a row is one
latitude, a column one longitude). Every target cell takes the value of
the one point nearest its centre, so no value is mixed with another.

An accumulation over the half hour ending at its time is published as the
mean rate over that half hour: millimetres times two, in mm/h, under
``prate`` (``SourceSpec.statistical_processes`` says it is a mean). A run
of the source is the newest issue alone: its four valid times, under the
hour the first of them falls in.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from .cmadust import write_series
from .errors import DownloadError
from .model import GfsRun
from .sources import SourceSpec

LOG = logging.getLogger(__name__)

MODEL = "hkonowcast"

URL = os.environ.get(
    "XUE_HKO_NOWCAST_URL",
    "https://data.weather.gov.hk/weatherAPI/hko_data/F3/Gridded_rainfall_nowcast.csv",
)

#: Hong Kong time, in which the file stamps every time.
HKT = timezone(timedelta(hours=8))
_TIME_FORMAT = "%Y%m%d%H%M"

#: The header the file carries, in its column order.
COLUMNS = (
    "Updated Date and Time (in Hong Kong Time)",
    "Ending Date and Time (in Hong Kong Time)",
    "Latitude (degree)",
    "Longitude (degree)",
    "Half-hourly Nowcast Accumulated Rainfall (mm)",
)

#: Minutes from the issue to the end of each accumulation.
LEAD_MINUTES = (30, 60, 90, 120)
#: Minutes each accumulation covers.
ACCUMULATION_MINUTES = 30

#: The regular grid the points are sampled onto, in thousandths of a degree:
#: the nearest whole thousandths to the file's longitude step and its mean
#: latitude step.
LONGITUDE_STEP_MILLIDEGREES = 19
LATITUDE_STEP_MILLIDEGREES = 18

#: Bytes read to learn the newest issue: the header and the first row.
HEAD_BYTES = 512

SERIES_UNIT = "mm/h"
FETCH_FILENAME = "fetch.json"


@dataclass(frozen=True)
class Issue:
    """One nowcast as the file holds it."""

    issue_time: datetime
    """When the nowcast was issued, UTC."""
    valid_times: tuple[datetime, ...]
    """The end of each accumulation, UTC, ascending."""
    latitudes: np.ndarray
    """The point rows' latitudes, ascending."""
    longitudes: np.ndarray
    """The point columns' longitudes, ascending."""
    accumulations: np.ndarray
    """``(time, latitude, longitude)`` millimetres, latitudes ascending."""


def parse_time(text: str) -> datetime:
    """A ``YYYYMMDDHHMM`` Hong Kong time as UTC."""
    try:
        return datetime.strptime(text.strip(), _TIME_FORMAT).replace(tzinfo=HKT).astimezone(UTC)
    except ValueError as exc:
        raise DownloadError(f"HKO nowcast time {text!r} is not YYYYMMDDHHMM") from exc


def _check_header(line: str) -> None:
    columns = tuple(column.strip() for column in line.lstrip("﻿").split(","))
    if columns != COLUMNS:
        raise DownloadError(f"HKO nowcast columns are {list(columns)}, not {list(COLUMNS)}")


def parse_head(text: str) -> datetime:
    """The issue time off the start of the file: the header and the first
    row, which is all a Range read of :data:`HEAD_BYTES` needs to hold."""
    lines = text.splitlines()
    if len(lines) < 2:
        raise DownloadError("the HKO nowcast head holds no data row")
    _check_header(lines[0])
    return parse_time(lines[1].split(",", 1)[0])


def parse_csv(text: str) -> Issue:
    """The whole file as an :class:`Issue`.

    One issue time; the accumulations' ends exactly :data:`LEAD_MINUTES`
    after it; every accumulation on the same points, every point once; no
    negative or missing value. Anything else is a truncated or changed
    file, refused rather than published with holes."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        raise DownloadError("the HKO nowcast holds no data row")
    _check_header(lines[0])
    issued: set[str] = set()
    ends: list[str] = []
    latitudes: list[float] = []
    longitudes: list[float] = []
    values: list[float] = []
    for number, line in enumerate(lines[1:], start=2):
        fields = line.split(",")
        if len(fields) != len(COLUMNS):
            raise DownloadError(f"HKO nowcast line {number} has {len(fields)} fields, not {len(COLUMNS)}")
        issued.add(fields[0].strip())
        ends.append(fields[1].strip())
        try:
            latitudes.append(float(fields[2]))
            longitudes.append(float(fields[3]))
            values.append(float(fields[4]))
        except ValueError as exc:
            raise DownloadError(f"HKO nowcast line {number} is not numeric: {line!r}") from exc
    if len(issued) != 1:
        raise DownloadError(f"the HKO nowcast holds {len(issued)} issue times, not one: {sorted(issued)}")
    issue_time = parse_time(issued.pop())
    end_names = sorted(set(ends))
    valid_times = tuple(parse_time(name) for name in end_names)
    expected = tuple(issue_time + timedelta(minutes=minutes) for minutes in LEAD_MINUTES)
    if valid_times != expected:
        raise DownloadError(
            f"the HKO nowcast issued {issue_time:%Y-%m-%dT%H:%MZ} ends its accumulations at "
            f"{[f'{moment:%H:%MZ}' for moment in valid_times]}, not "
            f"{[f'{moment:%H:%MZ}' for moment in expected]}"
        )
    lat = np.asarray(latitudes)
    lon = np.asarray(longitudes)
    value = np.asarray(values)
    unique_latitudes = np.unique(lat)
    unique_longitudes = np.unique(lon)
    shape = (len(valid_times), unique_latitudes.size, unique_longitudes.size)
    if value.size != shape[0] * shape[1] * shape[2]:
        raise DownloadError(
            f"the HKO nowcast holds {value.size} values, not {shape[0]} x {shape[1]} x {shape[2]}"
        )
    if not (np.isfinite(value).all() and (value >= 0).all()):
        raise DownloadError("the HKO nowcast holds a negative or non-finite accumulation")
    time_index = np.searchsorted(np.asarray(end_names), np.asarray(ends))
    row = np.searchsorted(unique_latitudes, lat)
    column = np.searchsorted(unique_longitudes, lon)
    accumulations = np.full(shape, np.nan)
    seen = np.zeros(shape, dtype=np.int32)
    np.add.at(seen, (time_index, row, column), 1)
    if (seen != 1).any():
        raise DownloadError("the HKO nowcast does not hold every point of every accumulation exactly once")
    accumulations[time_index, row, column] = value
    return Issue(
        issue_time=issue_time,
        valid_times=valid_times,
        latitudes=unique_latitudes,
        longitudes=unique_longitudes,
        accumulations=accumulations,
    )


def regular_axis(points: np.ndarray, step_millidegrees: int) -> np.ndarray:
    """The regular axis the points of one axis are sampled onto: the whole
    multiples of the step nearest the first and last point, and every one
    between. A centre on a whole multiple lies on the half-step lattice
    ``grid.snap_axis`` places a first centre on, and the coordinates are
    whole thousandths divided out at the end, as the snap writes them."""
    first = round(float(points[0]) * 1000.0 / step_millidegrees)
    last = round(float(points[-1]) * 1000.0 / step_millidegrees)
    return np.arange(first, last + 1, dtype=np.int64) * step_millidegrees / 1000.0


def nearest_points(points: np.ndarray, targets: np.ndarray) -> np.ndarray:
    """For each target coordinate, the index of the nearest point (the lower
    one on a tie), or -1 for a target more than half the end spacing beyond
    the outermost point. ``points`` ascend."""
    upper = np.clip(np.searchsorted(points, targets), 1, points.size - 1)
    lower = upper - 1
    index = np.where(targets - points[lower] <= points[upper] - targets, lower, upper)
    below = points[0] - (points[1] - points[0]) / 2.0
    above = points[-1] + (points[-1] - points[-2]) / 2.0
    return np.where((targets < below) | (targets > above), -1, index)


@dataclass(frozen=True)
class Window:
    """An issue sampled onto the regular grid, as mean rates."""

    latitudes: np.ndarray
    """Ascending."""
    longitudes: np.ndarray
    rates: np.ndarray
    """``(time, latitude, longitude)`` in mm/h, NaN where no point is near."""


def regrid(issue: Issue) -> Window:
    """The issue on the regular grid: each cell the nearest point's
    accumulation, as the mean rate over its half hour."""
    latitudes = regular_axis(issue.latitudes, LATITUDE_STEP_MILLIDEGREES)
    longitudes = regular_axis(issue.longitudes, LONGITUDE_STEP_MILLIDEGREES)
    rows = nearest_points(issue.latitudes, latitudes)
    columns = nearest_points(issue.longitudes, longitudes)
    sampled = issue.accumulations[:, np.clip(rows, 0, None)[:, None], np.clip(columns, 0, None)[None, :]]
    outside = (rows < 0)[:, None] | (columns < 0)[None, :]
    sampled[:, outside] = np.nan
    return Window(latitudes, longitudes, sampled * (60.0 / ACCUMULATION_MINUTES))


def first_hour(issue_time: datetime) -> datetime:
    """The run an issue is published under: the hour its first
    accumulation ends in."""
    return (issue_time + timedelta(minutes=LEAD_MINUTES[0])).replace(minute=0, second=0, microsecond=0)


def window_start(newest: datetime) -> datetime:
    """The run a live window starts at, from its newest valid time."""
    return first_hour(newest - timedelta(minutes=LEAD_MINUTES[-1]))


def latest_issue(*, fetch_head: Callable[[int], bytes]) -> datetime:
    """The newest issue the Observatory serves, off the first
    :data:`HEAD_BYTES` of the file."""
    head = fetch_head(HEAD_BYTES)
    return parse_head(head.decode("utf-8", errors="replace"))


def latest_slot(*, fetch_head: Callable[[int], bytes]) -> datetime:
    """The newest valid time: the end of the newest issue's last
    accumulation."""
    return latest_issue(fetch_head=fetch_head) + timedelta(minutes=LEAD_MINUTES[-1])


def run_is_complete(run: GfsRun, *, fetch_head: Callable[[int], bytes]) -> bool:
    """Whether a named window can be fetched: only the newest issue is
    served, so only the run it falls under."""
    return first_hour(latest_issue(fetch_head=fetch_head)) == run.time


def series_name(run: GfsRun) -> str:
    return f"{MODEL}.{run.id}.nc"


def series_attributes(issue: Issue, command: str) -> dict[str, str]:
    return {
        "Conventions": "CF-1.10",
        "title": "Hong Kong Observatory gridded rainfall nowcast",
        "institution": "Hong Kong Observatory",
        "source": (
            "Gridded_rainfall_nowcast.csv from DATA.GOV.HK; half-hourly accumulations as mean rates, "
            "sampled from the Mercator points onto a regular 0.019 x 0.018 degree grid by nearest point"
        ),
        "license": "Hong Kong Observatory (DATA.GOV.HK), https://data.gov.hk/en/terms-and-conditions",
        "history": f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} {command}",
        "forecast_reference_time": issue.issue_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write_issue(issue: Issue, path: Path, command: str) -> Window:
    """Write the issue's series: ``prate(time, latitude, longitude)`` in
    mm/h, time in hours from the issue (half hours are exact in binary, so
    every reader lands on the whole minute)."""
    window = regrid(issue)
    hours = [minutes / 60.0 for minutes in LEAD_MINUTES]
    path.parent.mkdir(parents=True, exist_ok=True)
    write_series(
        path,
        "prate",
        issue.issue_time,
        hours,  # type: ignore[arg-type] - fractional hours, read back exactly
        window.latitudes,
        window.longitudes,
        window.rates,
        series_attributes(issue, command),
        long_name="Mean precipitation rate over the half hour ending at the time (HKO nowcast)",
        units=SERIES_UNIT,
    )
    return window


def fetch_run(
    spec: SourceSpec,
    run: GfsRun,
    hours: int,
    raw_root: Path,
    *,
    force: bool,
    input_ids: tuple[str, ...] | None,
    fetch_bytes: Callable[[str], bytes],
) -> list[Path]:
    """Fetch the newest issue into ``raw_root/hkonowcast.<run>/``: the CSV
    as served, the series and a ``fetch.json`` in the shape the other
    windows leave (one entry per frame with its slot). The issue must fall
    under ``run``; one that has moved on since the run was resolved is
    refused, and the next round publishes it. Every round fetches afresh,
    so ``force`` changes nothing."""
    del force
    if input_ids is not None and any(variable_id not in spec.input_variable_ids for variable_id in input_ids):
        raise DownloadError(f"{spec.manifest_model} publishes {list(spec.input_variable_ids)}, not {list(input_ids)}")
    payload = fetch_bytes(URL)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DownloadError(f"the HKO nowcast is not UTF-8: {exc}") from exc
    issue = parse_csv(text)
    if first_hour(issue.issue_time) != run.time:
        raise DownloadError(
            f"the HKO nowcast served issue {issue.issue_time:%Y-%m-%dT%H:%MZ}, which falls under run "
            f"{GfsRun(first_hour(issue.issue_time)).id}, not {run.id}"
        )
    end = run.time + timedelta(hours=hours)
    if issue.valid_times[-1] > end:
        raise DownloadError(f"a {hours} h window from {run.id} ends before {issue.valid_times[-1]:%H:%MZ}")
    cadence = spec.cadence_seconds or 60
    if any(int(moment.timestamp()) % cadence for moment in issue.valid_times):
        raise DownloadError(
            f"the HKO nowcast issued {issue.issue_time:%H:%MZ} is off the {cadence} s marks the axis is counted in"
        )
    destination = raw_root / f"{spec.id}.{run.id}"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / f"{spec.id}.{issue.issue_time:%Y%m%d%H%M}.csv").write_bytes(payload)
    output = destination / series_name(run)
    window = write_issue(issue, output, f"xue fetch --model {spec.id} --run {run.id} --hours {hours}")
    record = {
        "model": spec.id,
        "run": run.id,
        "hours": hours,
        "cadenceSeconds": spec.cadence_seconds,
        "issueTime": issue.issue_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "grid": {"width": int(window.longitudes.size), "height": int(window.latitudes.size)},
        "series": output.name,
        "frames": [
            {"path": output.name, "slot": moment.strftime("%Y-%m-%dT%H:%M:%SZ")} for moment in issue.valid_times
        ],
    }
    (destination / FETCH_FILENAME).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    LOG.info(
        "%s issue %s: %d frames in %s",
        spec.manifest_model,
        issue.issue_time.strftime("%Y-%m-%dT%H:%MZ"),
        len(issue.valid_times),
        output,
    )
    return [output]
