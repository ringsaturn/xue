"""Daily area-weighted features of one run over one region
(``docs/indicators.md`` §4.1).

Inputs are physical values already read from the stores: 2 m temperature in
°C at instants, and precipitation rate in mm/h, taken to hold over the step
that ends at its frame (a GFS rate is instantaneous, an ECMWF, AIFS or IFS
HRES rate is the mean over that step; the indicators treat both the same
way). Days are local calendar days at the region's fixed UTC offset.

The operation order is part of the output and fixed:

1. A temperature frame belongs to the local day containing its valid time;
   a frame exactly at local midnight opens the new day.
2. Per day, in axis order: the area-weighted mean of each frame
   (``sum(w * T)`` over the window, NumPy's pairwise sum, float64), then
   ``t2m_mean`` = the plain mean of those frame means; per cell, the
   maximum and minimum over the day's frames, then ``t2m_max`` /
   ``t2m_min`` = their area-weighted means; ``hot30_frac`` /
   ``hot35_frac`` = the weight of the cells whose daily maximum is above
   30 / 35 °C.
3. Precipitation: each rate frame covers ``(previous offset, its offset]``
   (the first from the run time); the step is split over the local days it
   overlaps in proportion to the seconds in each, ``rate × hours`` per cell
   accumulated in axis order. ``precip`` = the area-weighted daily total,
   ``dry_frac`` = the weight of the cells whose daily total is below 1 mm.
4. ``gdd`` = ``max(0, min(t2m_mean, 30) − 10)`` from the unrounded mean.
5. Every value is rounded once, at the end, to 0.01 (Python ``round``,
   negative zero written as zero).

``complete`` is true when the day is wholly inside the run and every frame
the source's axis places in it is present: the run starts at or before the
day, every expected temperature frame in ``[start, end)`` is there, and
every expected precipitation frame in ``(start, close]`` is there, where
``close`` is the first expected offset at or after the day's end.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np

DAY_SECONDS = 86400
HOT30 = 30.0
HOT35 = 35.0
DRY_MM = 1.0
GDD_BASE = 10.0
GDD_CAP = 30.0
SUMMARY_DAYS = 14
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Series:
    """One variable over one window: frames at ``offsets_seconds`` after
    ``run_time``, values ``(frames, rows, cols)``."""

    run_time: datetime
    offsets_seconds: Sequence[int]
    values: np.ndarray

    def __post_init__(self) -> None:
        if self.values.ndim != 3 or self.values.shape[0] != len(self.offsets_seconds):
            raise ValueError("values must be (frames, rows, cols), one frame per offset")
        if any(after <= before for before, after in zip(self.offsets_seconds, self.offsets_seconds[1:])):
            raise ValueError("offsets must increase")


def round2(value: float) -> float:
    return round(float(value), 2) + 0.0


def _epoch_seconds(moment: datetime) -> int:
    return int((moment - _EPOCH).total_seconds())


def _local_day(instant: int, utc_offset_hours: int) -> int:
    """Days since 1970-01-01 of the local date of a UTC instant."""
    return (instant + utc_offset_hours * 3600) // DAY_SECONDS


def _day_bounds(day: int, utc_offset_hours: int) -> tuple[int, int]:
    """A local day's ``[start, end)`` in UTC epoch seconds."""
    start = day * DAY_SECONDS - utc_offset_hours * 3600
    return start, start + DAY_SECONDS


def _date(day: int) -> date:
    return date(1970, 1, 1) + timedelta(days=day)


def day_start(local_date: date, utc_offset_hours: int) -> datetime:
    """The UTC instant a local date begins at."""
    start, _ = _day_bounds((local_date - date(1970, 1, 1)).days, utc_offset_hours)
    return _EPOCH + timedelta(seconds=start)


def frame_steps(offsets_seconds: Sequence[int]) -> list[list[int]]:
    """An axis as ``[through_hour, step_hours]`` segments, the way a source
    table declares its steps: ``[[120, 1], [240, 3]]``."""
    segments: list[list[int]] = []
    for before, after in zip(offsets_seconds, offsets_seconds[1:]):
        step = (after - before) // 3600
        through = after // 3600
        if segments and segments[-1][1] == step:
            segments[-1][0] = through
        else:
            segments.append([through, step])
    if not segments:
        segments.append([offsets_seconds[0] // 3600, 1])
    return segments


def _weighted(weights: np.ndarray, field: np.ndarray) -> float:
    return float(np.sum(weights * field))


def _weight_where(weights: np.ndarray, mask: np.ndarray) -> float:
    return float(np.sum(np.where(mask, weights, 0.0)))


def daily_features(
    temperature: Series,
    precipitation: Series,
    weights: np.ndarray,
    utc_offset_hours: int,
    expected_offsets_seconds: Sequence[int] | None = None,
) -> list[dict[str, Any]]:
    """The §4.1 daily objects of one region, in date order. ``weights`` is
    the region's window of weights (summing to 1). ``expected_offsets_seconds``
    is the axis the source publishes for this run; None takes the
    temperature axis as given (no gap is then detectable)."""
    if temperature.values.shape[1:] != weights.shape or precipitation.values.shape[1:] != weights.shape:
        raise ValueError("every field must be the shape of the weights window")
    if temperature.run_time != precipitation.run_time:
        raise ValueError("both series must be of one run")
    run_start = _epoch_seconds(temperature.run_time)
    expected = list(expected_offsets_seconds if expected_offsets_seconds is not None else temperature.offsets_seconds)
    temperature_offsets = set(temperature.offsets_seconds)
    precipitation_offsets = set(precipitation.offsets_seconds)

    frames_by_day: dict[int, list[int]] = {}
    for index, offset in enumerate(temperature.offsets_seconds):
        frames_by_day.setdefault(_local_day(run_start + offset, utc_offset_hours), []).append(index)

    precipitation_by_day: dict[int, np.ndarray] = {}
    previous = 0
    for index, offset in enumerate(precipitation.offsets_seconds):
        begin, end = run_start + previous, run_start + offset
        previous = offset
        if end <= begin:
            continue
        rate = precipitation.values[index]
        for day in range(_local_day(begin, utc_offset_hours), _local_day(end - 1, utc_offset_hours) + 1):
            day_begin, day_end = _day_bounds(day, utc_offset_hours)
            overlap = min(end, day_end) - max(begin, day_begin)
            if overlap <= 0:
                continue
            total = precipitation_by_day.get(day)
            if total is None:
                total = precipitation_by_day[day] = np.zeros(weights.shape, dtype=np.float64)
            total += rate * (overlap / 3600.0)

    days: list[dict[str, Any]] = []
    for day in sorted(frames_by_day):
        indices = frames_by_day[day]
        frame_means = [_weighted(weights, temperature.values[index]) for index in indices]
        mean = 0.0
        for value in frame_means:
            mean += value
        mean /= len(frame_means)
        cell_max = temperature.values[indices[0]].astype(np.float64, copy=True)
        cell_min = cell_max.copy()
        for index in indices[1:]:
            np.maximum(cell_max, temperature.values[index], out=cell_max)
            np.minimum(cell_min, temperature.values[index], out=cell_min)
        rain = precipitation_by_day.get(day)
        if rain is None:
            rain = np.zeros(weights.shape, dtype=np.float64)
        start, end = _day_bounds(day, utc_offset_hours)
        days.append(
            {
                "date": _date(day).isoformat(),
                "t2m_mean": round2(mean),
                "t2m_max": round2(_weighted(weights, cell_max)),
                "t2m_min": round2(_weighted(weights, cell_min)),
                "precip": round2(_weighted(weights, rain)),
                "dry_frac": round2(_weight_where(weights, rain < DRY_MM)),
                "hot30_frac": round2(_weight_where(weights, cell_max > HOT30)),
                "hot35_frac": round2(_weight_where(weights, cell_max > HOT35)),
                "gdd": round2(max(0.0, min(mean, GDD_CAP) - GDD_BASE)),
                "complete": _complete(
                    start - run_start, end - run_start, expected, temperature_offsets, precipitation_offsets
                ),
            }
        )
    return days


def _complete(
    start: int, end: int, expected: list[int], temperature: set[int], precipitation: set[int]
) -> bool:
    """See the module docstring; ``start`` and ``end`` are seconds from the
    run time."""
    if start < 0:
        return False
    closing = [offset for offset in expected if offset >= end]
    if not closing:
        return False
    close = closing[0]
    if any(offset not in temperature for offset in expected if start <= offset < end):
        return False
    return all(offset in precipitation for offset in expected if start < offset <= close)


def summarize(days: Sequence[dict[str, Any]], limit: int = SUMMARY_DAYS) -> dict[str, Any]:
    """The line's per-region ``summary``: totals over the first ``limit``
    complete days, summed from the rounded daily values so a reader adds up
    the same numbers."""
    complete = [day for day in days if day["complete"]][:limit]
    precip = gdd = hot35 = 0.0
    for day in complete:
        precip += day["precip"]
        gdd += day["gdd"]
        hot35 += day["hot35_frac"]
    return {
        "days": len(complete),
        "precip_sum": round2(precip),
        "gdd_sum": round2(gdd),
        "hot35_days": round2(hot35),
    }
