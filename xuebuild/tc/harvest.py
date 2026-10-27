"""Fold the published hourly issues into the archive (``docs/tc.md`` §8.3):
read the last ``lookback`` hours of ``tc/YYYY/MM/DD/tc.<hour>/`` from the
data origin, keep every agency and model forecast of a numbered system,
and the newest working best tracks; IBTrACS supplies the agencies' best
tracks beside them.

An issue's identities are the ones the product gave at the time. A system
first tracked as an invest or a model's own find carries a synthetic id
until it is numbered; the crosswalk the later issues publish maps it to
the ATCF id, and its earlier forecasts — the ones that make a genesis
score — are read back over ``genesis`` hours and filed under that id.
Systems never numbered do not enter the archive.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

from ..errors import DownloadError, TcProductError
from ..fetch import _http_error_code, get_ok
from . import ibtracs
from .archive import Archive, make_best, make_record
from .fetch import IBTRACS_URL
from .registry import canonical_basin, season_of
from .schema import ATCF_ID, INDEX_FILENAME, issue_directory

LOG = logging.getLogger(__name__)

LOOKBACK_HOURS = 48
GENESIS_HOURS = 240
"""How far back a newly numbered system's synthetic-id forecasts are read:
ECMWF finds a system up to ten days before it is numbered. The hourly
directories must be kept at least this long."""
WORKERS = 8
IBTRACS_LAST3_URL = IBTRACS_URL.replace("ACTIVE", "last3years")


def _get_json(base_url: str, relative: str, crc: str | None = None) -> dict[str, Any] | None:
    url = urllib.parse.urljoin(base_url, relative) + (f"?v={crc}" if crc else "")
    try:
        return json.loads(get_ok(url, attempts=3, timeout=120))
    except DownloadError as exc:
        if _http_error_code(exc) in (403, 404):
            return None
        raise


def _follow(crosswalk: dict[str, str], storm: str) -> str:
    seen = {storm}
    while storm in crosswalk and crosswalk[storm] not in seen:
        storm = crosswalk[storm]
        seen.add(storm)
    return storm


def canonical_storm(storm: str, base: str) -> str:
    """The id an issue's ATCF id stands for. Issues built before the
    tracker's sub-basins were folded (``BB02`` for JTWC's ``IO02``, ``SI02``
    for ``SH02``) and before the southern season was counted from July
    carry the calendar year; ``base`` (a forecast's) gives the season back.
    A southern id already in its July-to-June season is left alone."""
    basin, number, year = storm[:2], storm[2:4], int(storm[4:])
    canonical = canonical_basin(basin)
    time = datetime.fromisoformat(base)
    if canonical == "SH" and (basin != canonical or year == time.year):
        year = season_of("SH", time)
    return f"{canonical}{number}{year}"


def storm_records(storm: dict[str, Any], target: str) -> list[dict[str, Any]]:
    """A storm file's forecasts as archive records under ``target``."""
    records = []
    sections = [("agency", source, f) for source, f in storm.get("agencies", {}).items()]
    sections += [("ensemble" if "members" in f else "model", source, f) for source, f in storm.get("models", {}).items()]
    for kind, source, forecast in sections:
        storm_id = canonical_storm(target, forecast["base"])
        records.append(make_record(storm_id, storm_id[:2], storm.get("name"), source, kind, "live", forecast))
    return records


def harvest_issues(
    archive: Archive,
    base_url: str,
    now: datetime,
    *,
    lookback: int = LOOKBACK_HOURS,
    genesis: int = GENESIS_HOURS,
) -> dict[str, Any]:
    """Read the issues in the ``lookback`` hours up to ``now`` (and, for
    the systems they renumber, back over ``genesis`` hours), merge their
    forecasts into ``archive`` and their working best tracks into its
    seasons. Returns a summary."""
    base_url = base_url.rstrip("/") + "/"
    end = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)

    def hours(span: int, skip: int = 0) -> list[datetime]:
        return [end - timedelta(hours=h) for h in range(skip, span + 1)]

    def indexes(hour_list: list[datetime]) -> dict[datetime, dict[str, Any]]:
        with ThreadPoolExecutor(WORKERS) as pool:
            found = pool.map(lambda h: _get_json(base_url, f"{issue_directory(h)}/{INDEX_FILENAME}"), hour_list)
        return {h: index for h, index in zip(hour_list, found) if index is not None}

    recent = indexes(hours(lookback))
    crosswalk: dict[str, str] = {}
    for hour in sorted(recent):
        crosswalk.update(recent[hour].get("crosswalk", {}))
    renamed = {old for old, new in crosswalk.items() if ATCF_ID.match(_follow(crosswalk, new))}
    older = indexes(hours(genesis, lookback + 1)) if renamed and genesis > lookback else {}

    wanted: list[tuple[datetime, str, dict[str, Any]]] = []
    for hour, index in sorted({**older, **recent}.items()):
        for entry in index["storms"]:
            target = _follow(crosswalk, entry["id"])
            if not ATCF_ID.match(target) or (hour in older and entry["id"] not in renamed):
                continue
            wanted.append((hour, target, entry))

    def storm_file(item: tuple[datetime, str, dict[str, Any]]) -> dict[str, Any] | None:
        hour, _, entry = item
        return _get_json(base_url, f"{issue_directory(hour)}/{entry['path']}", entry["crc32"])

    with ThreadPoolExecutor(WORKERS) as pool:
        storms = list(pool.map(storm_file, wanted))

    records = []
    working: dict[str, dict[str, Any]] = {}
    for (hour, target, entry), storm in zip(wanted, storms):
        if storm is None:
            LOG.warning("tc archive: %s of %s is listed but missing", entry["path"], hour)
            continue
        records.extend(storm_records(storm, target))
        if storm.get("best") and entry["id"] == target:
            working[target] = storm
    changed = archive.add_records(records)
    for target, storm in working.items():
        best = {
            key: track for key, track in storm["best"].items() if track.get("source") != "ibtracs" and track["points"]
        }
        if best:
            last = max(track["points"][-1]["time"] for track in best.values())
            storm_id = canonical_storm(target, last)
            archive.add_best(make_best(storm_id, storm.get("name"), storm.get("sid"), [storm_id], best))
    return {
        "issues": len(recent) + len(older),
        "renamed": sorted(renamed),
        "records": len(records),
        "changed": changed,
        "workingBest": sorted(working),
    }


def ibtracs_best(text: str, *, first_season: int | None = None) -> list[dict[str, Any]]:
    """Every storm of an IBTrACS CSV that carries an ATCF id, as archive
    best records under its newest id; ``first_season`` drops older ones."""
    records = []
    for storm in ibtracs.parse_ibtracs(text).values():
        if storm.atcf_id is None or not ATCF_ID.match(storm.atcf_id):
            continue
        if first_season is not None and int(storm.atcf_id[-4:]) < first_season:
            continue
        best = {
            key: {**track.to_json(), "source": "ibtracs"}
            for key, track in storm.agencies.items()
            if track.points
        }
        if best:
            ids = [atcf_id for atcf_id in storm.atcf_ids if ATCF_ID.match(atcf_id)]
            records.append(make_best(storm.atcf_id, storm.name, storm.sid, ids, best))
    return records


def harvest_ibtracs(archive: Archive, url: str = IBTRACS_LAST3_URL, *, first_season: int | None = None) -> int:
    text = get_ok(url, attempts=3, timeout=600).decode("utf-8", errors="replace")
    return add_best_records(archive, ibtracs_best(text, first_season=first_season))


def add_best_records(archive: Archive, records: list[dict[str, Any]]) -> int:
    """Merge best records, skipping (with a warning) a storm whose rows
    fail validation rather than losing the run to one bad row."""
    added = 0
    for record in records:
        try:
            archive.add_best(record)
        except TcProductError as exc:
            LOG.warning("tc archive: best track of %s skipped: %s", record["storm"], exc)
            continue
        added += 1
    return added
