"""The tropical cyclone archive (``docs/tc.md`` §8): every distinct
forecast of a numbered system, filed by the month of its base time, and
every season's best tracks — the record a track-error score is computed
from, where the hourly issue directories are the live view that a week
later is gone.

Layout under ``tc/archive/`` in the data root::

    catalog.json                  the one mutable entry point
    <YYYY>/<MM>/forecasts.jsonl   one forecast per line, sorted by (storm, source, base)
    <YYYY>/<MM>/index.json        each storm's [offset, length] in the month's lines
    <YYYY>/best.jsonl             one storm per line: its best tracks, every agency key
    <YYYY>/index.json             each storm's [offset, length] in best.jsonl and its months

A month or season file is rewritten in place when something new reaches
it, and is always named by its CRC32 from the file above it, so a reader
requests ``<path>?v=<crc32>`` exactly as for the hourly product. Nothing in
these files carries a timestamp of the build: the same records give the
same bytes, and an unchanged month is not uploaded again.

This module owns the shapes, the merge and the local tree; what feeds it is
:mod:`.harvest` (the published hourly issues and IBTrACS) and
:mod:`.backfill` (the agencies' and ECMWF's own archives).
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import time
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..errors import DownloadError, TcProductError
from ..fetch import _http_error_code, get_ok
from .identity import MATCH_KM, distance_km
from .registry import season_of
from .schema import (
    ATCF_ID,
    PRODUCT,
    SOURCE_KEY,
    crc32_hex,
    encode_json,
    validate_ensemble,
    validate_forecast,
    validate_track,
    write_bytes_atomic,
)
from .track import MISSING, iso_z

LOG = logging.getLogger(__name__)

ARCHIVE_SCHEMA_VERSION = 1
ARCHIVE_DIRECTORY = f"{PRODUCT}/archive"
CATALOG_FILENAME = "catalog.json"
INDEX_FILENAME = "index.json"
FORECASTS_FILENAME = "forecasts.jsonl"
BEST_FILENAME = "best.jsonl"
OVERVIEW_FILENAME = "overview.json"
SCORES_FILENAME = "scores.json"

PULL_ATTEMPTS = 4
KINDS = ("agency", "model", "ensemble")
ORIGINS = ("live", "nhc-atcf", "ucar-atcf", "ecmwf-aws")
"""Where a record came from. ``live`` is the hourly product; the others
are the backfill sources (:mod:`.backfill`)."""
_ORIGIN_RANK = {"live": 0}
"""A live record is what the product showed at the time; a backfilled one
only fills the gaps around it."""

MONTH = re.compile(r"^(\d{4})-(\d{2})$")


def month_of(base: str) -> str:
    """``2026-10``: the UTC month of an ISO ``base`` a record is filed in."""
    return base[:7]


def month_directory(month: str) -> str:
    year, number = month.split("-")
    return f"{ARCHIVE_DIRECTORY}/{year}/{number}"


def season_directory(season: int) -> str:
    return f"{ARCHIVE_DIRECTORY}/{season:04d}"


def season_of_id(storm: str) -> int:
    return int(storm[-4:])


# --- records -----------------------------------------------------------


def make_record(
    storm: str, basin: str, name: str | None, source: str, kind: str, origin: str, forecast: dict[str, Any]
) -> dict[str, Any]:
    """One line of a month: a forecast (tc v1 ``Forecast``, or ``Ensemble``
    when ``kind`` is ``ensemble``) of one numbered storm by one source."""
    return {
        "storm": storm,
        "basin": basin,
        "name": name,
        "source": source,
        "kind": kind,
        "origin": origin,
        "forecast": forecast,
    }


def record_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return record["storm"], record["source"], record["forecast"]["base"]


def validate_record(record: object, label: str) -> None:
    if not isinstance(record, dict):
        raise TcProductError(f"{label} must be an object")
    storm = record.get("storm")
    if not isinstance(storm, str) or not ATCF_ID.match(storm):
        raise TcProductError(f"{label}.storm must be an ATCF id")
    if record.get("basin") != storm[:2]:
        raise TcProductError(f"{label}.basin must be the storm id's basin")
    name = record.get("name")
    if name is not None and not isinstance(name, str):
        raise TcProductError(f"{label}.name must be a string or null")
    source = record.get("source")
    if not isinstance(source, str) or not SOURCE_KEY.match(source):
        raise TcProductError(f"{label}.source must be a source id")
    if record.get("kind") not in KINDS:
        raise TcProductError(f"{label}.kind must be one of {', '.join(KINDS)}")
    if record.get("origin") not in ORIGINS:
        raise TcProductError(f"{label}.origin must be one of {', '.join(ORIGINS)}")
    if record["kind"] == "ensemble":
        validate_ensemble(record.get("forecast"), f"{label}.forecast")
    else:
        validate_forecast(record.get("forecast"), f"{label}.forecast")


def merge_records(existing: dict[tuple[str, str, str], dict[str, Any]], records: Iterable[dict[str, Any]]) -> int:
    """Fold ``records`` into ``existing`` (keyed by :func:`record_key`), in
    order: a record replaces the one under its key unless that one is live
    and it is not. Returns how many keys changed."""
    changed = 0
    for record in records:
        key = record_key(record)
        current = existing.get(key)
        if current is not None:
            if _ORIGIN_RANK.get(record["origin"], 1) > _ORIGIN_RANK.get(current["origin"], 1):
                continue
            if current == record:
                continue
        existing[key] = record
        changed += 1
    return changed


def choose_track(candidates: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """The best track to keep for one agency key out of the archived one,
    IBTrACS' and a centre's working file: IBTrACS once it is final, else
    whichever reaches furthest, the earlier candidate on a tie."""
    chosen: dict[str, Any] | None = None
    for track in candidates:
        if not track or not track.get("points"):
            continue
        if track.get("source") == "ibtracs" and not track.get("provisional"):
            return track
        if chosen is None or track["points"][-1]["time"] > chosen["points"][-1]["time"]:
            chosen = track
    return chosen


# The Saffir-Simpson class floors in knots, TD to H5, the storm map's own
# (`web/src/tc/intensity.ts`): an overview point carries its class index.
SSHS_FLOORS_KT = (0, 34, 64, 83, 96, 113, 137)
KNOT = 0.514444


def sshs_index(vmax: float | None) -> int:
    """0 (TD) to 6 (H5) for a one-minute wind in m/s, -1 for none."""
    if vmax is None:
        return -1
    kt = vmax / KNOT
    return max(index for index, floor in enumerate(SSHS_FLOORS_KT) if kt >= floor - 0.5)


def overview_track(record: dict[str, Any]) -> list[list[int]]:
    """A storm's track for a season overview: the US best track (else the
    first with points) at its synoptic times, as `[lon×10, lat×10, class]`."""
    keys = sorted(record["best"], key=lambda key: (key != "usa", key))
    for key in keys:
        points = [
            point
            for point in record["best"][key]["points"]
            if point["time"][11:13] in ("00", "06", "12", "18") and point["time"][14:16] == "00"
        ]
        if points:
            return [[round(point["lon"] * 10), round(point["lat"] * 10), sshs_index(point["vmax"])] for point in points]
    return []


def best_summary(record: dict[str, Any]) -> dict[str, Any]:
    """A season index's one-line summary of a storm's best tracks: the
    first and last time any of them places it, and its peak wind on the
    US track (one-minute), else on the first key that has one."""
    times = [point["time"] for track in record["best"].values() for point in track["points"]]
    vmax = None
    for key in sorted(record["best"], key=lambda k: (k != "usa", k)):
        winds = [point["vmax"] for point in record["best"][key]["points"] if point["vmax"] is not None]
        if winds:
            vmax = max(winds)
            break
    return {"first": min(times) if times else None, "last": max(times) if times else None, "vmax": vmax}


def make_best(
    storm: str, name: str | None, sid: str | None, atcf: list[str], best: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    return {
        "storm": storm,
        "basin": storm[:2],
        "name": name,
        "sid": sid,
        "atcf": sorted(set(atcf) | {storm}),
        "best": {key: best[key] for key in sorted(best)},
    }


def validate_best(record: object, label: str) -> None:
    if not isinstance(record, dict):
        raise TcProductError(f"{label} must be an object")
    storm = record.get("storm")
    if not isinstance(storm, str) or not ATCF_ID.match(storm):
        raise TcProductError(f"{label}.storm must be an ATCF id")
    atcf = record.get("atcf")
    if not isinstance(atcf, list) or storm not in atcf or any(not ATCF_ID.match(str(i)) for i in atcf):
        raise TcProductError(f"{label}.atcf must list ATCF ids including the storm's own")
    best = record.get("best")
    if not isinstance(best, dict):
        raise TcProductError(f"{label}.best must be an object")
    for key, track in best.items():
        if not SOURCE_KEY.match(key):
            raise TcProductError(f"{label}.best has a malformed key {key!r}")
        validate_track(track, f"{label}.best.{key}")


# --- the local tree ----------------------------------------------------


@dataclass
class Archive:
    """The archive under ``root`` (the data root), optionally mirroring a
    published one at ``base_url``: the catalog and every index are pulled
    on open, a month's or season's lines when they are first touched, each
    only when the local copy's CRC32 differs from what the catalog names."""

    root: Path
    base_url: str | None = None
    catalog: dict[str, Any] = field(default_factory=dict)
    months: dict[str, dict[tuple[str, str, str], dict[str, Any]]] = field(default_factory=dict)
    """Loaded months' records; only these are rewritten."""
    seasons: dict[int, dict[str, dict[str, Any]]] = field(default_factory=dict)
    """Loaded seasons' best records by storm; only these are rewritten."""
    written: list[str] = field(default_factory=list)

    @classmethod
    def open(cls, root: Path, base_url: str | None = None) -> Archive:
        archive = cls(root=root, base_url=base_url.rstrip("/") + "/" if base_url else None)
        archive.catalog = archive._pull_catalog()
        for entry in archive.catalog.get("months", []) + archive.catalog.get("seasons", []):
            archive._pull(f"{ARCHIVE_DIRECTORY}/{entry['path']}", entry["crc32"])
        return archive

    # pulling

    def _url(self, relative: str, crc: str | None) -> str:
        assert self.base_url is not None
        url = urllib.parse.urljoin(self.base_url, relative)
        return f"{url}?v={crc}" if crc else url

    def _pull_catalog(self) -> dict[str, Any]:
        local = self.root / ARCHIVE_DIRECTORY / CATALOG_FILENAME
        if self.base_url is not None:
            # The catalog is the one mutable name; a timestamp query keeps
            # an edge from answering with yesterday's.
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            try:
                body = get_ok(self._url(f"{ARCHIVE_DIRECTORY}/{CATALOG_FILENAME}", f"t{stamp}"), attempts=3)
            except DownloadError as exc:
                if _http_error_code(exc) not in (403, 404):
                    raise
                LOG.info("tc archive: no published catalog, starting a fresh archive")
                return _empty_catalog()
            write_bytes_atomic(local, body)
        if not local.exists():
            return _empty_catalog()
        catalog = json.loads(local.read_text(encoding="utf-8"))
        validate_catalog(catalog)
        return catalog

    def _pull(self, relative: str, crc: str) -> Path:
        path = self.root / relative
        if path.exists() and crc32_hex(path.read_bytes()) == crc:
            return path
        if self.base_url is None:
            raise TcProductError(f"{path} is missing or is not CRC32 {crc}, and no --base-url to pull it from")
        # A month is tens of megabytes; a connection the edge drops midway
        # (IncompleteRead) or a short body is fetched again, not fatal.
        problem: Exception | None = None
        for attempt in range(PULL_ATTEMPTS):
            if attempt:
                time.sleep(5 * attempt)
            try:
                body = get_ok(self._url(relative, crc), attempts=3, timeout=300)
            except (DownloadError, http.client.HTTPException, OSError) as exc:
                problem = exc
                LOG.warning("tc archive: pulling %s failed (%s), attempt %d", relative, exc, attempt + 1)
                continue
            if crc32_hex(body) != crc:
                problem = TcProductError(f"{relative} arrived as CRC32 {crc32_hex(body)}, not the {crc} its index names")
                LOG.warning("tc archive: %s", problem)
                continue
            write_bytes_atomic(path, body)
            return path
        raise TcProductError(f"could not pull {relative}: {problem}")

    def _entry(self, section: str, key: str, value: Any) -> dict[str, Any] | None:
        return next((e for e in self.catalog.get(section, []) if e[key] == value), None)

    # months

    def month(self, month: str) -> dict[tuple[str, str, str], dict[str, Any]]:
        if month not in self.months:
            records: dict[tuple[str, str, str], dict[str, Any]] = {}
            if self._entry("months", "month", month) is not None:
                index = self.month_index(month)
                lines = self._pull(f"{month_directory(month)}/{FORECASTS_FILENAME}", index["forecasts"]["crc32"])
                for line in lines.read_bytes().splitlines():
                    record = json.loads(line)
                    records[record_key(record)] = record
            self.months[month] = records
        return self.months[month]

    def month_index(self, month: str) -> dict[str, Any]:
        return json.loads((self.root / month_directory(month) / INDEX_FILENAME).read_text(encoding="utf-8"))

    def pull_month_lines(self, month: str) -> Path:
        return self._pull(
            f"{month_directory(month)}/{FORECASTS_FILENAME}", self.month_index(month)["forecasts"]["crc32"]
        )

    def add_records(self, records: Iterable[dict[str, Any]]) -> int:
        """Merge records into their months (loading each on first touch)."""
        changed = 0
        for record in records:
            validate_record(record, f"record {record.get('storm')} {record.get('source')}")
            changed += merge_records(self.month(month_of(record["forecast"]["base"])), [record])
        return changed

    # seasons

    def season(self, season: int) -> dict[str, dict[str, Any]]:
        if season not in self.seasons:
            storms: dict[str, dict[str, Any]] = {}
            if self._entry("seasons", "season", season) is not None:
                index = self.season_index(season)
                lines = self._pull(f"{season_directory(season)}/{BEST_FILENAME}", index["best"]["crc32"])
                for line in lines.read_bytes().splitlines():
                    record = json.loads(line)
                    storms[record["storm"]] = record
            self.seasons[season] = storms
        return self.seasons[season]

    def season_index(self, season: int) -> dict[str, Any]:
        return json.loads((self.root / season_directory(season) / INDEX_FILENAME).read_text(encoding="utf-8"))

    def pull_best_lines(self, season: int) -> Path:
        return self._pull(f"{season_directory(season)}/{BEST_FILENAME}", self.season_index(season)["best"]["crc32"])

    def add_best(self, record: dict[str, Any]) -> None:
        """Merge one storm's best tracks into its season, key by key."""
        validate_best(record, f"best {record.get('storm')}")
        storms = self.season(season_of_id(record["storm"]))
        current = storms.get(record["storm"])
        if current is None:
            storms[record["storm"]] = record
            return
        keys = set(current["best"]) | set(record["best"])
        best = {}
        for key in keys:
            track = choose_track([record["best"].get(key), current["best"].get(key)])
            if track is not None:
                best[key] = track
        storms[record["storm"]] = make_best(
            record["storm"],
            record["name"] or current["name"],
            record["sid"] or current["sid"],
            current["atcf"] + record["atcf"],
            best,
        )

    # writing

    def write_file(self, relative: str, payload: bytes) -> None:
        """Write one file of the tree, listing it in ``written`` only when
        its bytes changed."""
        self._write(relative, payload)

    def write_scores_catalog(self, seasons: list[int]) -> dict[str, Any]:
        """Name the scores just written for ``seasons`` in the catalog
        (``docs/tc.md`` §9) and write it."""
        entries = {entry["season"]: entry for entry in self.catalog.get("scores", [])}
        for season in seasons:
            body = (self.root / season_directory(season) / SCORES_FILENAME).read_bytes()
            entries[season] = {
                "season": season,
                "path": f"{season:04d}/{SCORES_FILENAME}",
                "byteLength": len(body),
                "crc32": crc32_hex(body),
                "from": json.loads(body)["from"],
            }
        catalog = {**self.catalog, "scores": [entries[season] for season in sorted(entries)]}
        validate_catalog(catalog)
        self._write(f"{ARCHIVE_DIRECTORY}/{CATALOG_FILENAME}", encode_json(catalog))
        self.catalog = catalog
        return catalog

    def _write(self, relative: str, payload: bytes) -> None:
        path = self.root / relative
        if path.exists() and path.read_bytes() == payload:
            return
        write_bytes_atomic(path, payload)
        # Listed at its last write, so a file written twice (the catalog,
        # once for the archive and once for the scores) is uploaded once,
        # after everything the second version names.
        if relative in self.written:
            self.written.remove(relative)
        self.written.append(relative)

    def write(self) -> dict[str, Any]:
        """Write every loaded month and season that changed, then the
        catalog. ``written`` lists the files in upload order: each before
        the file that names it, the catalog last."""
        for month in sorted(self.months):
            if self.months[month]:
                self._write_month(month)
        # A season's index lists the months its storms appear in, so every
        # season a written month touches is rewritten too.
        touched = {season_of_id(storm) for month in self.months for storm, _, _ in self.months[month]}
        for season in sorted(touched | set(self.seasons)):
            self._write_season(season)
        catalog = self._catalog()
        validate_catalog(catalog)
        self._write(f"{ARCHIVE_DIRECTORY}/{CATALOG_FILENAME}", encode_json(catalog))
        self.catalog = catalog
        return catalog

    def _write_month(self, month: str) -> None:
        records = [self.months[month][key] for key in sorted(self.months[month])]
        lines = bytearray()
        storms: list[dict[str, Any]] = []
        for record in records:
            line = encode_json(record) + b"\n"
            if not storms or storms[-1]["id"] != record["storm"]:
                storms.append(
                    {"id": record["storm"], "basin": record["basin"], "name": None, "offset": len(lines), "length": 0,
                     "records": 0, "sources": []}
                )
            entry = storms[-1]
            entry["name"] = record["name"] or entry["name"]
            entry["length"] += len(line)
            entry["records"] += 1
            if record["source"] not in entry["sources"]:
                entry["sources"].append(record["source"])
            lines += line
        for entry in storms:
            entry["sources"].sort()
        body = bytes(lines)
        index = {
            "archiveSchemaVersion": ARCHIVE_SCHEMA_VERSION,
            "product": PRODUCT,
            "month": month,
            "forecasts": {"path": FORECASTS_FILENAME, "byteLength": len(body), "crc32": crc32_hex(body)},
            "storms": storms,
        }
        validate_month_index(index)
        self._write(f"{month_directory(month)}/{FORECASTS_FILENAME}", body)
        self._write(f"{month_directory(month)}/{INDEX_FILENAME}", encode_json(index))

    def _storm_months(self, season: int) -> dict[str, tuple[list[str], set[str]]]:
        """Each storm of the season → the months it has forecasts in and the
        sources of those forecasts, from every month index on disk (the
        catalog's, plus those just written)."""
        months = {e["month"] for e in self.catalog.get("months", [])} | {m for m in self.months if self.months[m]}
        result: dict[str, tuple[list[str], set[str]]] = {}
        for month in sorted(months):
            for entry in self.month_index(month)["storms"]:
                if season_of_id(entry["id"]) == season:
                    found = result.setdefault(entry["id"], ([], set()))
                    found[0].append(month)
                    found[1].update(entry["sources"])
        return result

    def _write_season(self, season: int) -> None:
        storms = self.season(season)
        months = self._storm_months(season)
        lines = bytearray()
        entries: dict[str, dict[str, Any]] = {}
        for storm in sorted(storms):
            record = storms[storm]
            line = encode_json(record) + b"\n"
            in_months, sources = months.get(storm, ([], set()))
            entries[storm] = {
                "id": storm,
                "basin": record["basin"],
                "name": record["name"],
                "sid": record["sid"],
                "atcf": record["atcf"],
                "offset": len(lines),
                "length": len(line),
                "best": sorted(record["best"]),
                **best_summary(record),
                "months": in_months,
                "sources": sorted(sources),
            }
            lines += line
        for storm in sorted(set(months) - set(entries)):
            in_months, sources = months[storm]
            entries[storm] = {
                "id": storm, "basin": storm[:2], "name": None, "sid": None, "atcf": [storm],
                "offset": None, "length": None, "best": [], "first": None, "last": None, "vmax": None,
                "months": in_months, "sources": sorted(sources),
            }
        if not entries:
            return
        body = bytes(lines)
        # Every storm's track in one small file, for a picture of the season
        # (the showcase card) that needs no best-track spans.
        overview = encode_json(
            {
                "archiveSchemaVersion": ARCHIVE_SCHEMA_VERSION,
                "product": PRODUCT,
                "season": season,
                "storms": [
                    {"id": storm, "track": track}
                    for storm in sorted(storms)
                    if (track := overview_track(storms[storm]))
                ],
            }
        )
        index = {
            "archiveSchemaVersion": ARCHIVE_SCHEMA_VERSION,
            "product": PRODUCT,
            "season": season,
            "best": {"path": BEST_FILENAME, "byteLength": len(body), "crc32": crc32_hex(body)},
            "overview": {"path": OVERVIEW_FILENAME, "byteLength": len(overview), "crc32": crc32_hex(overview)},
            "storms": [entries[storm] for storm in sorted(entries)],
        }
        validate_season_index(index)
        self._write(f"{season_directory(season)}/{BEST_FILENAME}", body)
        self._write(f"{season_directory(season)}/{OVERVIEW_FILENAME}", overview)
        self._write(f"{season_directory(season)}/{INDEX_FILENAME}", encode_json(index))

    def _catalog(self) -> dict[str, Any]:
        months: dict[str, dict[str, Any]] = {e["month"]: e for e in self.catalog.get("months", [])}
        seasons: dict[int, dict[str, Any]] = {e["season"]: e for e in self.catalog.get("seasons", [])}
        for month in self.months:
            path = self.root / month_directory(month) / INDEX_FILENAME
            if path.exists():
                body = path.read_bytes()
                index = json.loads(body)
                months[month] = {
                    "month": month,
                    "path": f"{month[:4]}/{month[5:]}/{INDEX_FILENAME}",
                    "byteLength": len(body),
                    "crc32": crc32_hex(body),
                    "storms": len(index["storms"]),
                    "records": sum(entry["records"] for entry in index["storms"]),
                }
        for season in self._written_seasons():
            path = self.root / season_directory(season) / INDEX_FILENAME
            if path.exists():
                body = path.read_bytes()
                seasons[season] = {
                    "season": season,
                    "path": f"{season:04d}/{INDEX_FILENAME}",
                    "byteLength": len(body),
                    "crc32": crc32_hex(body),
                    "storms": len(json.loads(body)["storms"]),
                }
        catalog: dict[str, Any] = {
            "archiveSchemaVersion": ARCHIVE_SCHEMA_VERSION,
            "product": PRODUCT,
            "months": [months[m] for m in sorted(months)],
            "seasons": [seasons[s] for s in sorted(seasons)],
        }
        # A season's scores stay named until they are recomputed; their
        # ``from`` tells a reader (and the next run) whether they are current.
        if self.catalog.get("scores"):
            catalog["scores"] = self.catalog["scores"]
        return catalog

    def _written_seasons(self) -> set[int]:
        seasons = set(self.seasons)
        for month in self.months:
            seasons |= {season_of_id(storm) for storm, _, _ in self.months[month]}
        return seasons


def _empty_catalog() -> dict[str, Any]:
    return {"archiveSchemaVersion": ARCHIVE_SCHEMA_VERSION, "product": PRODUCT, "months": [], "seasons": []}


# --- placing a forecast on a best track ----------------------------------


class BestMatcher:
    """Which archived storm a forecast's starting position belongs to: the
    season's best-track storm (any agency key, the US one first) whose
    synoptic position at that time lies within ``MATCH_KM`` of it, the
    nearest when two do. For sources whose own storm numbers cannot be
    trusted (ECMWF's in the western Pacific)."""

    def __init__(self, archive: Archive) -> None:
        self.archive = archive
        self._positions: dict[int, dict[str, dict[str, tuple[float, float]]]] = {}

    def _season(self, season: int) -> dict[str, dict[str, tuple[float, float]]]:
        if season not in self._positions:
            storms: dict[str, dict[str, tuple[float, float]]] = {}
            known = {entry["season"] for entry in self.archive.catalog.get("seasons", [])} | set(self.archive.seasons)
            if season in known:
                for storm, record in self.archive.season(season).items():
                    positions: dict[str, tuple[float, float]] = {}
                    for key in sorted(record["best"], key=lambda k: (k != "usa", k)):
                        for point in record["best"][key]["points"]:
                            positions.setdefault(point["time"], (point["lat"], point["lon"]))
                    storms[storm] = positions
            self._positions[season] = storms
        return self._positions[season]

    def forget(self) -> None:
        """Drop what was read, after best tracks were added."""
        self._positions.clear()

    def match(self, basin: str, time: str, lat: float, lon: float) -> str | None:
        found = self.nearest(basin, time, lat, lon)
        return None if found is None else found[1]

    def nearest(self, basin: str, time: str, lat: float, lon: float) -> tuple[float, str] | None:
        """(distance in km, storm) of the match, or None."""
        when = datetime.fromisoformat(time)
        found: tuple[float, str] | None = None
        for season in sorted({season_of(basin, when) + offset for offset in (-1, 0, 1)}):
            for storm, positions in self._season(season).items():
                if storm[:2] != basin or time not in positions:
                    continue
                at = positions[time]
                distance = distance_km(lat, lon, at[0], at[1])
                if distance <= MATCH_KM and (found is None or distance < found[0]):
                    found = (distance, storm)
        return found


def start_position(kind: str, forecast: dict[str, Any]) -> tuple[str, float, float] | None:
    """(time, lat, lon) a forecast starts from: its first point, or for an
    ensemble the centroid of its members at their first shared lead."""
    if kind != "ensemble":
        if not forecast["points"]:
            return None
        point = forecast["points"][0]
        return point["time"], point["lat"], point["lon"]
    leads = len(forecast["leads"])
    for position, lead in enumerate(forecast["leads"]):
        lats, lons = [], []
        for member in range(len(forecast["members"])):
            index = member * leads + position
            if forecast["lat"][index] != MISSING and forecast["lon"][index] != MISSING:
                lats.append(forecast["lat"][index] / 100)
                lons.append(forecast["lon"][index] / 100)
        if lats:
            anchor = lons[0]
            lon = sum(anchor + ((x - anchor + 540.0) % 360.0 - 180.0) for x in lons) / len(lons)
            lon = (lon + 540.0) % 360.0 - 180.0
            base = datetime.fromisoformat(forecast["base"])
            return iso_z(base + timedelta(seconds=lead)), sum(lats) / len(lats), 180.0 if lon == -180.0 else lon
    return None


def rekey(archive: Archive, matcher: BestMatcher, months: Iterable[str], sources: set[str], basins: set[str]) -> dict[str, int]:
    """Move the archived forecasts of ``sources`` in ``basins`` to the storm
    their start places them on (:class:`BestMatcher`); one that matches no
    storm is dropped. On a collision the record starting nearer the best
    track is kept. Returns how many moved and were dropped."""
    moved = dropped = 0
    for month in months:
        if not any(e["month"] == month for e in archive.catalog.get("months", [])):
            continue
        records = archive.month(month)
        kept: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
        for key, record in records.items():
            distance = 0.0
            if record["source"] in sources and record["basin"] in basins:
                start = start_position(record["kind"], record["forecast"])
                found = None if start is None else matcher.nearest(record["basin"], *start)
                if found is None:
                    dropped += 1
                    continue
                distance, target = found
                if target != record["storm"]:
                    record = {**record, "storm": target, "basin": target[:2]}
                    moved += 1
                    key = record_key(record)
            current = kept.get(key)
            if current is None or distance < current[0]:
                kept[key] = (distance, record)
        archive.months[month] = {key: record for key, (_, record) in kept.items()}
    return {"moved": moved, "dropped": dropped}


# --- validation of the indexes ------------------------------------------


def _check_head(payload: object, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TcProductError(f"{label} must be an object")
    if payload.get("archiveSchemaVersion") != ARCHIVE_SCHEMA_VERSION:
        raise TcProductError(f"{label} archiveSchemaVersion must be {ARCHIVE_SCHEMA_VERSION}")
    if payload.get("product") != PRODUCT:
        raise TcProductError(f"{label} product must be {PRODUCT!r}")
    return payload


def _check_file(entry: object, label: str, name: str) -> None:
    if not isinstance(entry, dict) or entry.get("path") != name:
        raise TcProductError(f"{label} must name {name}")
    length = entry.get("byteLength")
    if isinstance(length, bool) or not isinstance(length, int) or length < 0:
        raise TcProductError(f"{label}.byteLength must be a non-negative integer")
    if not isinstance(entry.get("crc32"), str) or not re.match(r"^[0-9a-f]{8}$", entry["crc32"]):
        raise TcProductError(f"{label}.crc32 must be 8 lowercase hex characters")


def _check_span(entry: dict[str, Any], label: str, total: int, previous_end: int, *, optional: bool = False) -> int:
    offset, length = entry.get("offset"), entry.get("length")
    if optional and offset is None and length is None:
        return previous_end
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (offset, length)) or length <= 0:
        raise TcProductError(f"{label} must carry a positive byte span")
    if offset != previous_end or offset + length > total:
        raise TcProductError(f"{label} span must follow the previous one inside the file")
    return offset + length


def validate_month_index(payload: object) -> None:
    index = _check_head(payload, "month index")
    match = MONTH.match(str(index.get("month")))
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise TcProductError("month index month must be YYYY-MM")
    _check_file(index.get("forecasts"), "month index forecasts", FORECASTS_FILENAME)
    storms = index.get("storms")
    if not isinstance(storms, list):
        raise TcProductError("month index storms must be a list")
    end = 0
    previous = ""
    for position, entry in enumerate(storms):
        label = f"month index storms[{position}]"
        if not isinstance(entry, dict) or not ATCF_ID.match(str(entry.get("id"))):
            raise TcProductError(f"{label}.id must be an ATCF id")
        if entry["id"] <= previous:
            raise TcProductError("month index storms must be sorted by id, each once")
        previous = entry["id"]
        end = _check_span(entry, label, index["forecasts"]["byteLength"], end)
    if end != index["forecasts"]["byteLength"]:
        raise TcProductError("month index spans must cover the whole forecasts file")


def validate_season_index(payload: object) -> None:
    index = _check_head(payload, "season index")
    season = index.get("season")
    if isinstance(season, bool) or not isinstance(season, int) or not 1800 <= season <= 2200:
        raise TcProductError("season index season must be a year")
    _check_file(index.get("best"), "season index best", BEST_FILENAME)
    if "overview" in index:
        _check_file(index["overview"], "season index overview", OVERVIEW_FILENAME)
    storms = index.get("storms")
    if not isinstance(storms, list):
        raise TcProductError("season index storms must be a list")
    end = 0
    previous = ""
    for position, entry in enumerate(storms):
        label = f"season index storms[{position}]"
        if not isinstance(entry, dict) or not ATCF_ID.match(str(entry.get("id"))):
            raise TcProductError(f"{label}.id must be an ATCF id")
        if season_of_id(entry["id"]) != season:
            raise TcProductError(f"{label} is not a storm of season {season}")
        if entry["id"] <= previous:
            raise TcProductError("season index storms must be sorted by id, each once")
        previous = entry["id"]
        end = _check_span(entry, label, index["best"]["byteLength"], end, optional=True)
        months = entry.get("months")
        if not isinstance(months, list) or any(not MONTH.match(str(m)) for m in months):
            raise TcProductError(f"{label}.months must list YYYY-MM months")
    if end != index["best"]["byteLength"]:
        raise TcProductError("season index spans must cover the whole best file")


def validate_catalog(payload: object) -> None:
    catalog = _check_head(payload, "tc archive catalog")
    for section, key in (("months", "month"), ("seasons", "season"), ("scores", "season")):
        entries = catalog.get(section, [] if section == "scores" else None)
        if not isinstance(entries, list):
            raise TcProductError(f"tc archive catalog {section} must be a list")
        previous: Any = None
        for position, entry in enumerate(entries):
            label = f"tc archive catalog {section}[{position}]"
            if not isinstance(entry, dict):
                raise TcProductError(f"{label} must be an object")
            value = entry.get(key)
            if previous is not None and value <= previous:
                raise TcProductError(f"tc archive catalog {section} must be sorted, each once")
            previous = value
            if section == "months":
                expected = f"{value[:4]}/{value[5:]}/{INDEX_FILENAME}"
            else:
                expected = f"{value:04d}/{SCORES_FILENAME if section == 'scores' else INDEX_FILENAME}"
            _check_file(entry, label, expected)
            if section == "scores" and not isinstance(entry.get("from"), dict):
                raise TcProductError(f"{label}.from must say what the scores were computed from")
