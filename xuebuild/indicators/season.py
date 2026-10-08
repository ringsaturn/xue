"""In-season totals per region (``docs/indicators.md`` §4.2), from GFS only.

Each local day of a season is taken from the newest GFS run that started at
or before the day began and covers the whole day (its line marks the day
``complete``): the cycle within six hours before the day opens when it was
indexed, an older one when a cycle is missing, in which case the day is
``partial``. That is the first-day forecast standing in for an analysis;
it uses only what the feature lines already hold, so a season file is
rebuilt from the lines and the previous season file alone, and rebuilt to
the same bytes. ECMWF and AIFS do not enter: their 3- and 6-hourly frames
make a coarser day than GFS's hourly ones, and their runs are not archived.

A day counts once the newest indexed run is the cycle that should supply
it or a later one (it started less than six hours before the day, or
after), so that cycle has had its chance; a day already counted is
replaced only by a newer run that qualifies. The totals follow from the
daily rows in date order:

- ``precip_total``, ``gdd_total``, ``hot35_days`` (the sum of
  ``hot35_frac``): sums of the rounded daily values, rounded to 0.01;
- ``cdd_current`` / ``cdd_max``: consecutive days whose area-weighted
  precipitation is below 1 mm; a day missing from the rows ends a spell;
- ``*_window``: the same over the days inside the flowering window.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from . import calendar
from .features import DRY_MM, day_start, round2
from .regions import REGIONS, region
from .schema import SCHEMA_VERSION, RANGES

SEASON_SOURCE = "gfs"
IDEAL_LEAD_HOURS = 6
"""A day supplied by a run that started less than this before it is not
partial: the four GFS cycles are six hours apart."""

SeasonKey = tuple[str, str]
"""``(region id, season id)``."""


def season_path(region_id: str, season: str) -> str:
    return f"season/{region_id}.{season}.json"


def _run_time(run: str) -> datetime:
    return datetime.strptime(run, "%Y%m%d%H").replace(tzinfo=UTC)


def _candidates(lines: Iterable[Mapping[str, Any]]) -> tuple[dict[tuple[str, str], dict[str, Any]], str | None]:
    """The best row per ``(region, date)`` from the lines, and the newest run."""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    newest: str | None = None
    for line in lines:
        if line.get("source") != SEASON_SOURCE:
            continue
        run = line["run"]
        newest = run if newest is None or run > newest else newest
        run_time = _run_time(run)
        for region_id, block in line["regions"].items():
            offset = region(region_id).utc_offset_hours
            for day in block["days"]:
                if not day["complete"]:
                    continue
                local = date.fromisoformat(day["date"])
                opens = day_start(local, offset)
                if run_time > opens:
                    continue
                key = (region_id, day["date"])
                current = best.get(key)
                if current is not None and current["run"] >= run:
                    continue
                lead = int((opens - run_time).total_seconds() // 3600)
                best[key] = {
                    "date": day["date"],
                    "run": run,
                    "lead_hours": lead,
                    "partial": lead >= IDEAL_LEAD_HOURS,
                    "flowering": calendar.in_window(region_id, local, "flowering"),
                    **{field: day[field] for field in RANGES},
                    "complete": True,
                }
    return best, newest


def _totals(region_id: str, daily: list[dict[str, Any]]) -> dict[str, Any]:
    precip = gdd = hot35 = 0.0
    precip_window = hot35_window = 0.0
    window_days = partial = 0
    current = longest = 0
    previous: date | None = None
    for day in daily:
        local = date.fromisoformat(day["date"])
        precip += day["precip"]
        gdd += day["gdd"]
        hot35 += day["hot35_frac"]
        partial += day["partial"]
        if day["flowering"]:
            window_days += 1
            precip_window += day["precip"]
            hot35_window += day["hot35_frac"]
        if day["precip"] < DRY_MM:
            current = current + 1 if previous is not None and local - previous == timedelta(days=1) else 1
        else:
            current = 0
        longest = max(longest, current)
        previous = local
    return {
        "days_counted": len(daily),
        "days_partial": partial,
        "precip_total": round2(precip),
        "gdd_total": round2(gdd),
        "hot35_days": round2(hot35),
        "cdd_current": current,
        "cdd_max": longest,
        "days_counted_window": window_days,
        "precip_window": round2(precip_window),
        "hot35_days_window": round2(hot35_window),
    }


def build_seasons(
    lines: Iterable[Mapping[str, Any]],
    previous: Mapping[SeasonKey, Mapping[str, Any]],
    *,
    weights_version: str,
) -> dict[SeasonKey, dict[str, Any]]:
    """Every season file the lines and the previous files support, keyed by
    ``(region, season)``: the previous rows merged with what the lines
    supply (the newer run wins a date), cut to the days the newest run has
    reached."""
    best, newest = _candidates(lines)
    through = newest
    for payload in previous.values():
        if through is None or payload["throughRun"] > through:
            through = payload["throughRun"]
    if through is None:
        return {}
    reached = _run_time(newest) if newest is not None else None

    rows: dict[SeasonKey, dict[str, dict[str, Any]]] = {}
    for (region_id, season), payload in previous.items():
        for day in payload["daily"]:
            rows.setdefault((region_id, season), {})[day["date"]] = dict(day)
    for (region_id, day_text), row in best.items():
        local = date.fromisoformat(day_text)
        season = calendar.season_of(region_id, local)
        if season is None or reached is None:
            continue
        if day_start(local, region(region_id).utc_offset_hours) - timedelta(hours=IDEAL_LEAD_HOURS) >= reached:
            continue
        existing = rows.setdefault((region_id, season), {}).get(day_text)
        if existing is None or existing["run"] <= row["run"]:
            rows[(region_id, season)][day_text] = row

    order = {entry.id: rank for rank, entry in enumerate(REGIONS)}
    files: dict[SeasonKey, dict[str, Any]] = {}
    for key in sorted(rows, key=lambda item: (order[item[0]], item[1])):
        region_id, season = key
        daily = [rows[key][day_text] for day_text in sorted(rows[key])]
        if not daily:
            continue
        crop = calendar.CALENDARS[region_id]
        files[key] = {
            "schemaVersion": SCHEMA_VERSION,
            "region": region_id,
            "season": season,
            "source": SEASON_SOURCE,
            "calendar": {"version": calendar.VERSION},
            "weights": {"version": weights_version},
            "windows": {"season": crop.season.spelled(), "flowering": crop.flowering.spelled()},
            "throughRun": through,
            **_totals(region_id, daily),
            "daily": daily,
        }
    return files
