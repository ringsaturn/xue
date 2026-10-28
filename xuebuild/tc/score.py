"""Track and intensity errors of the archived forecasts against the season
best tracks (``docs/tc.md`` §9): one ``scores.json`` and one
``errors.jsonl`` per season beside its best tracks, recomputed when the
season's best tracks or any month its storms appear in has changed.

The verification follows the centres' own practice: a forecast is verified
at a lead only when the system is a tropical or subtropical cyclone in the
best track both when the forecast starts and when it verifies; positions
are compared at the synoptic hours only (IBTrACS' three-hourly rows are
interpolations); an ensemble is verified by the mean of its members'
positions where at least half of them still carry the system. Each truth
(``usa``, and in the western Pacific ``jma`` and ``cma``) is scored on its
own and never mixed with another: they differ in position, and in wind
averaging (one minute for ``usa``, ten for the others).
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

from .archive import INDEX_FILENAME, Archive, month_directory, season_directory
from .harvest import clustered_ensemble
from .schema import PRODUCT, crc32_hex, encode_json
from .track import MISSING

LOG = logging.getLogger(__name__)

SCORE_SCHEMA_VERSION = 1
SCORES_FILENAME = "scores.json"
ERRORS_FILENAME = "errors.jsonl"
LEADS_HOURS = (0, 12, 24, 36, 48, 72, 96, 120, 144, 168, 192, 216, 240)
SYNOPTIC_HOURS = (0, 6, 12, 18)
EARTH_RADIUS_KM = 6371.0
TROPICAL_MIN_WIND = 12.86
"""25 kt in m/s: a point whose source gives no class counts as a tropical
cyclone from this wind up (JTWC's half of IBTrACS' USA columns carries no
status)."""

TRUTHS: dict[str, tuple[str, ...]] = {
    "usa": ("AL", "EP", "CP", "WP", "IO", "SH"),
    "jma": ("WP",),
    "cma": ("WP",),
}
"""Best-track key → the basins it is a truth in."""
TROPICAL_CLASSES: dict[str, frozenset[str]] = {
    # USA_STATUS: depression, storm, typhoon, super typhoon, hurricane,
    # major hurricane, subtropical depression and storm.
    "usa": frozenset({"TD", "TS", "TY", "ST", "STY", "TC", "HU", "HR", "MH", "SD", "SS"}),
    # TOKYO_GRADE 2–5 (TD to TY); 9 is "TS or stronger" in old records.
    "jma": frozenset({"2", "3", "4", "5", "9"}),
    # CMA_CAT 1–6 (TD to super typhoon); 0 is below TD, 9 extratropical.
    "cma": frozenset({"1", "2", "3", "4", "5", "6"}),
}
REFERENCES: dict[str, tuple[str, ...]] = {
    "AL": ("nhc", "ecmwf"),
    "EP": ("nhc", "ecmwf"),
    "CP": ("nhc", "ecmwf"),
    "WP": ("jtwc", "ecmwf"),
    "IO": ("jtwc", "ecmwf"),
    "SH": ("jtwc", "ecmwf"),
}
"""The sources each basin's homogeneous comparison is made against: the
official forecast, and ECMWF's deterministic run."""
ENSEMBLE_QUORUM = 0.5


# --- geometry ------------------------------------------------------------


def great_circle_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _local_xy(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """East and north km of (lat, lon) from (lat0, lon0) on the tangent
    plane, longitude difference taken the short way round."""
    dlon = (lon - lon0 + 540.0) % 360.0 - 180.0
    k = math.pi * EARTH_RADIUS_KM / 180.0
    return dlon * k * math.cos(math.radians(lat0)), (lat - lat0) * k


def along_cross(
    truth: tuple[float, float], forecast: tuple[float, float], motion: tuple[float, float] | None
) -> tuple[float | None, float | None]:
    """The error's components along the best track's motion (positive:
    forecast ahead) and across it (positive: forecast to the right).
    ``motion`` is the (east, north) displacement the truth was making."""
    if motion is None:
        return None, None
    mx, my = motion
    norm = math.hypot(mx, my)
    if norm < 1e-6:
        return None, None
    ex, ey = _local_xy(truth[0], truth[1], forecast[0], forecast[1])
    ux, uy = mx / norm, my / norm
    return ex * ux + ey * uy, ex * uy - ey * ux


def mean_position(lats: list[float], lons: list[float]) -> tuple[float, float]:
    """The centroid of a few positions, longitudes unwrapped about the
    first so a set straddling the antimeridian stays together."""
    anchor = lons[0]
    unwrapped = [anchor + ((lon - anchor + 540.0) % 360.0 - 180.0) for lon in lons]
    lon = statistics.fmean(unwrapped)
    lon = (lon + 540.0) % 360.0 - 180.0
    return statistics.fmean(lats), 180.0 if lon == -180.0 else lon


# --- the truth -----------------------------------------------------------


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def truth_points(track: dict[str, Any], key: str) -> dict[datetime, dict[str, Any]]:
    """A best track's synoptic points by time, each marked ``tropical``."""
    classes = TROPICAL_CLASSES.get(key, frozenset())
    points = {}
    for point in track["points"]:
        time = _parse_time(point["time"])
        if time.minute or time.hour not in SYNOPTIC_HOURS:
            continue
        cls = point.get("class")
        vmax = point.get("vmax")
        tropical = cls in classes if cls is not None else vmax is not None and vmax >= TROPICAL_MIN_WIND
        points[time] = {**point, "tropical": tropical}
    return points


def _motion(points: dict[datetime, dict[str, Any]], time: datetime) -> tuple[float, float] | None:
    """The truth's displacement through ``time``: from six hours before to
    six after, or the one side that exists."""
    before = points.get(time - timedelta(hours=6))
    after = points.get(time + timedelta(hours=6))
    start, end = before or points.get(time), after or points.get(time)
    if start is None or end is None or start is end:
        return None
    return _local_xy(start["lat"], start["lon"], end["lat"], end["lon"])


# --- the forecasts -------------------------------------------------------


def forecast_positions(record: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Lead hours → {lat, lon, vmax, pmin} of a record at the scored leads.
    An ensemble gives its member mean where a quorum carries the system."""
    forecast = record["forecast"]
    wanted = set(LEADS_HOURS)
    positions: dict[int, dict[str, Any]] = {}
    # A track with nothing past its start is a system the model lost (ECMWF
    # writes such a one, often at a stale position): not a forecast at all.
    leads = [point["lead"] for point in forecast["points"]] if record["kind"] != "ensemble" else forecast["leads"]
    if not any(lead > 0 for lead in leads):
        return positions
    if record["kind"] != "ensemble":
        for point in forecast["points"]:
            hours, rest = divmod(point["lead"], 3600)
            if not rest and hours in wanted:
                positions[hours] = {k: point.get(k) for k in ("lat", "lon", "vmax", "pmin")}
        return positions
    forecast = clustered_ensemble(forecast)
    leads = forecast["leads"]
    members = len(forecast["members"])
    if not members:
        return positions
    for position, lead in enumerate(leads):
        hours, rest = divmod(lead, 3600)
        if rest or hours not in wanted:
            continue
        lats, lons, vmaxes, pmins = [], [], [], []
        for member in range(members):
            index = member * len(leads) + position
            lat, lon = forecast["lat"][index], forecast["lon"][index]
            if lat == MISSING or lon == MISSING:
                continue
            lats.append(lat / 100)
            lons.append(lon / 100)
            if forecast["vmax"][index] != MISSING:
                vmaxes.append(forecast["vmax"][index] / 10)
            if forecast["pmin"][index] != MISSING:
                pmins.append(forecast["pmin"][index] / 10)
        if len(lats) < ENSEMBLE_QUORUM * members:
            continue
        lat, lon = mean_position(lats, lons)
        positions[hours] = {
            "lat": lat,
            "lon": lon,
            "vmax": statistics.fmean(vmaxes) if vmaxes else None,
            "pmin": statistics.fmean(pmins) if pmins else None,
        }
    return positions


# --- scoring one storm ----------------------------------------------------


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def score_storm(
    records: list[dict[str, Any]], best: dict[str, dict[str, Any]], basin: str
) -> dict[str, dict[str, dict[str, list[Any]]]]:
    """``{truth: {source: {base, lead, track, along, cross, vmax, pmin}}}``:
    one column per field, one row per verified (base, lead); vmax and pmin
    are forecast minus truth, null where either side has none."""
    result: dict[str, dict[str, dict[str, list[Any]]]] = {}
    for key, basins in TRUTHS.items():
        if basin not in basins or key not in best:
            continue
        truth = truth_points(best[key], key)
        if not truth:
            continue
        columns: dict[str, dict[str, list[Any]]] = {}
        for record in sorted(records, key=lambda r: (r["source"], r["forecast"]["base"])):
            base = _parse_time(record["forecast"]["base"])
            start = truth.get(base)
            if start is None or not start["tropical"]:
                continue
            for hours, position in sorted(forecast_positions(record).items()):
                time = base + timedelta(hours=hours)
                verifying = truth.get(time)
                if verifying is None or not verifying["tropical"] or position["lat"] is None:
                    continue
                along, cross = along_cross(
                    (verifying["lat"], verifying["lon"]), (position["lat"], position["lon"]), _motion(truth, time)
                )
                column = columns.setdefault(
                    record["source"],
                    {"base": [], "lead": [], "track": [], "along": [], "cross": [], "vmax": [], "pmin": []},
                )
                column["base"].append(record["forecast"]["base"])
                column["lead"].append(hours)
                column["track"].append(
                    _round(great_circle_km(verifying["lat"], verifying["lon"], position["lat"], position["lon"]))
                )
                column["along"].append(_round(along))
                column["cross"].append(_round(cross))
                dv = (
                    None
                    if position["vmax"] is None or verifying.get("vmax") is None
                    else position["vmax"] - verifying["vmax"]
                )
                dp = (
                    None
                    if position["pmin"] is None or verifying.get("pmin") is None
                    else position["pmin"] - verifying["pmin"]
                )
                column["vmax"].append(_round(dv))
                column["pmin"].append(_round(dp))
        if columns:
            result[key] = columns
    return result


# --- aggregation -----------------------------------------------------------


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 1) if values else None


def summarise(storms: dict[str, tuple[str, dict[str, Any]]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Per (basin, truth, source, lead): the sample size and mean errors;
    and per (basin, truth, reference, source, lead) the homogeneous
    comparison — only the cases both sources verified."""
    samples: dict[tuple[str, str, str, int], list[dict[str, Any]]] = {}
    by_case: dict[tuple[str, str, str, str, int], dict[str, float]] = {}
    for storm, (basin, truths) in storms.items():
        for truth, sources in truths.items():
            for source, column in sources.items():
                for row in range(len(column["lead"])):
                    lead = column["lead"][row]
                    sample = {field: column[field][row] for field in ("track", "along", "cross", "vmax", "pmin")}
                    samples.setdefault((basin, truth, source, lead), []).append(sample)
                    by_case.setdefault((basin, truth, storm, column["base"][row], lead), {})[source] = sample["track"]
    summary = []
    for (basin, truth, source, lead), rows in sorted(samples.items()):
        tracks = [r["track"] for r in rows]
        vmax = [r["vmax"] for r in rows if r["vmax"] is not None]
        pmin = [r["pmin"] for r in rows if r["pmin"] is not None]
        summary.append(
            {
                "basin": basin,
                "truth": truth,
                "source": source,
                "lead": lead,
                "n": len(rows),
                "track": _mean(tracks),
                "trackMedian": round(statistics.median(tracks), 1),
                "along": _mean([r["along"] for r in rows if r["along"] is not None]),
                "cross": _mean([r["cross"] for r in rows if r["cross"] is not None]),
                "nVmax": len(vmax),
                "vmaxBias": _mean(vmax),
                "vmaxMae": _mean([abs(v) for v in vmax]),
                "nPmin": len(pmin),
                "pminBias": _mean(pmin),
                "pminMae": _mean([abs(p) for p in pmin]),
            }
        )
    pairs: dict[tuple[str, str, str, str, int], tuple[list[float], list[float]]] = {}
    for (basin, truth, _, _, lead), tracks in by_case.items():
        for reference in REFERENCES.get(basin, ()):
            if reference not in tracks:
                continue
            for source, error in tracks.items():
                if source == reference:
                    continue
                own, theirs = pairs.setdefault((basin, truth, reference, source, lead), ([], []))
                own.append(error)
                theirs.append(tracks[reference])
    homogeneous = []
    for (basin, truth, reference, source, lead), (own, theirs) in sorted(pairs.items()):
        mean_own, mean_reference = statistics.fmean(own), statistics.fmean(theirs)
        homogeneous.append(
            {
                "basin": basin,
                "truth": truth,
                "reference": reference,
                "source": source,
                "lead": lead,
                "n": len(own),
                "track": round(mean_own, 1),
                "referenceTrack": round(mean_reference, 1),
                "skill": round(1 - mean_own / mean_reference, 3) if mean_reference > 0 else None,
            }
        )
    return summary, homogeneous


# --- the season files ------------------------------------------------------


def season_signature(archive: Archive, season: int) -> dict[str, Any]:
    """What a season's scores are a function of: its index (the best
    tracks and the months its storms appear in) and those months'
    indexes, each by CRC32."""
    index_bytes = (archive.root / season_directory(season) / INDEX_FILENAME).read_bytes()
    index = json.loads(index_bytes)
    months = sorted({month for entry in index["storms"] for month in entry["months"]})
    return {
        "season": crc32_hex(index_bytes),
        "months": {
            month: crc32_hex((archive.root / month_directory(month) / INDEX_FILENAME).read_bytes()) for month in months
        },
    }


def _storm_records(archive: Archive, month: str, storms: set[str]) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """(storm, its records) for the given storms in one month, read span
    by span so a month is never held whole."""
    index = archive.month_index(month)
    path = archive.pull_month_lines(month)
    with path.open("rb") as handle:
        for entry in index["storms"]:
            if entry["id"] not in storms:
                continue
            handle.seek(entry["offset"])
            body = handle.read(entry["length"])
            yield entry["id"], [json.loads(line) for line in body.splitlines()]


def score_season(archive: Archive, season: int) -> dict[str, Any]:
    """Write ``scores.json`` and ``errors.jsonl`` of one season; returns
    the scores document."""
    index = archive.season_index(season)
    best_lines = archive.pull_best_lines(season)
    best_by_id: dict[str, dict[str, Any]] = {}
    with best_lines.open("rb") as handle:
        for line in handle:
            record = json.loads(line)
            for atcf_id in record["atcf"]:
                best_by_id[atcf_id] = record
    months: dict[str, set[str]] = {}
    for entry in index["storms"]:
        for month in entry["months"]:
            months.setdefault(month, set()).add(entry["id"])
    records: dict[str, list[dict[str, Any]]] = {}
    for month in sorted(months):
        for storm, found in _storm_records(archive, month, months[month]):
            records.setdefault(storm, []).extend(found)
    scored: dict[str, tuple[str, dict[str, Any]]] = {}
    for storm in sorted(records):
        best = best_by_id.get(storm)
        if best is None:
            continue
        truths = score_storm(records[storm], best["best"], storm[:2])
        if truths:
            scored[storm] = (storm[:2], truths)
    lines = bytearray()
    entries = []
    for storm, (basin, truths) in scored.items():
        line = encode_json({"storm": storm, "basin": basin, "truths": truths}) + b"\n"
        entries.append({"id": storm, "offset": len(lines), "length": len(line)})
        lines += line
    summary, homogeneous = summarise(scored)
    body = bytes(lines)
    scores = {
        "scoreSchemaVersion": SCORE_SCHEMA_VERSION,
        "product": PRODUCT,
        "season": season,
        "from": season_signature(archive, season),
        "leads": list(LEADS_HOURS),
        "errors": {"path": ERRORS_FILENAME, "byteLength": len(body), "crc32": crc32_hex(body)},
        "storms": entries,
        "summary": summary,
        "homogeneous": homogeneous,
    }
    archive.write_file(f"{season_directory(season)}/{ERRORS_FILENAME}", body)
    archive.write_file(f"{season_directory(season)}/{SCORES_FILENAME}", encode_json(scores))
    return scores


def score_stale(archive: Archive, *, seasons: list[int] | None = None) -> dict[str, Any]:
    """Score every season (or the given ones) whose signature differs from
    the one its published scores were computed from, then write the
    catalog. Returns a summary."""
    catalog = archive.catalog
    published = {entry["season"]: entry for entry in catalog.get("scores", [])}
    candidates = seasons if seasons is not None else [entry["season"] for entry in catalog.get("seasons", [])]
    scored = []
    for season in candidates:
        signature = season_signature(archive, season)
        entry = published.get(season)
        if entry is not None and entry.get("from") == signature and seasons is None:
            continue
        document = score_season(archive, season)
        scored.append(season)
        LOG.info("tc score %d: %d storms, %d summary rows", season, len(document["storms"]), len(document["summary"]))
    if scored:
        archive.write_scores_catalog(scored)
    return {"scored": scored}

