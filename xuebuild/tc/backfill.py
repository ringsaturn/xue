"""Backfill the archive (``docs/tc.md`` §8.4) from the archives the
centres keep themselves, for the months before the live harvest began:

- ``nhc``: NHC's ATCF a-decks (``atcf/archive/<YYYY>/``, the current
  season in ``aid_public/``) for the Atlantic and the two Pacific basins —
  the official forecast and the objective aids;
- ``ucar``: the RAL mirror of the ATCF a-decks for the western Pacific,
  north Indian and southern basins (models only: JTWC's own forecast is
  not in the public decks);
- ``ecmwf``: the ``tf`` BUFR of ECMWF open data as kept on AWS
  (``s3://ecmwf-forecasts``, from 2023-01-18), every basin;
- ``ibtracs``: the agencies' best tracks, every storm of the seasons asked
  for, numbered or not;
- ``hko``: the Hong Kong Observatory's last bulletin on each system of the
  seasons asked for, which it keeps serving after the system is gone: its
  past positions are the Observatory's working best track, ``best.hko``.

Each record is filed with its ``origin``; a live record of the same key
always stays (:func:`.archive.merge_records`). Only numbered systems' forecasts
are kept: an a-deck's file id, or ECMWF's ``stormIdentifier`` up to 49.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..errors import DownloadError, TcProductError
from ..fetch import _http_error_code, get_ok
from . import atcf, hko
from .archive import Archive, BestMatcher, make_best, make_record, month_of, start_position
from .build import ecmwf_tracks
from .fetch import HKO_TRACK_URL
from .harvest import add_best_records, ibtracs_best
from .registry import NUMBERED_MAX
from .schema import ATCF_ID
from .track import Ensemble, Forecast, lead_seconds, member_from_points

LOG = logging.getLogger(__name__)

SOURCES = ("nhc", "ucar", "ecmwf", "ibtracs", "curated", "hko")

CURATED_FILE = Path(__file__).with_name("curated.json")
"""Tracks reconstructed for this archive, each naming its sources."""

NHC_ARCHIVE_URL = "https://ftp.nhc.noaa.gov/atcf/archive"
NHC_CURRENT_URL = "https://ftp.nhc.noaa.gov/atcf/aid_public"
UCAR_ADECK_URL = "https://hurricanes.ral.ucar.edu/repository/data/adecks_open"
ECMWF_AWS_URL = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
IBTRACS_SINCE1980_URL = (
    "https://www.ncei.noaa.gov/data/international-best-track-archive-for-climate-stewardship-ibtracs"
    "/v04r01/access/csv/ibtracs.since1980.list.v04r01.csv"
)
IBTRACS_ALL_URL = IBTRACS_SINCE1980_URL.replace("since1980", "ALL")
IBTRACS_SINCE1980_FROM = 1980
ECMWF_FIRST_CYCLE = datetime(2023, 1, 18, tzinfo=UTC)
ECMWF_RESOLUTIONS = ("ifs/0p25", "0p4-beta")
"""Where a cycle's files sit on the bucket: ``0p4-beta`` before the 0.25°
upgrade (2024-02), ``ifs/0p25`` after."""
ECMWF_STREAMS = {"ecmwf": ("oper", "scda"), "ecmwfens": ("enfo",)}
"""HRES is ``oper`` from 00 and 12 UTC and ``scda`` from 06 and 18."""

# An a-deck's techs → (source, kind). The ensembles are a member set per
# base, the member number the tech's own (``AP07`` is member 7, the
# control ``AC00`` / ``EC00`` member 0) and ``AEMN`` / ``EEMN`` the mean.
SINGLE_TECHS = {
    "OFCL": ("nhc", "agency"),
    "AVNO": ("gfs", "model"),
    "EMX": ("ecmwf", "model"),
    "GDMN": ("gdm", "model"),
}
EMBARGOED_SOURCES = {"gdm": timedelta(hours=48)}
"""Sources archived only once a forecast is history: Google DeepMind's
tracks are CC BY 4.0 for times past and under Google's real-time terms
(which the archive cannot meet, and which exclude some countries
outright) for anything still ahead. A track is kept once its last valid
time is this long past, the README's 48 hours rather than the terms' one."""


def released(record: dict[str, Any], now: datetime) -> bool:
    """Whether a record may be archived now (`EMBARGOED_SOURCES`)."""
    embargo = EMBARGOED_SOURCES.get(record["source"])
    if embargo is None:
        return True
    points = record["forecast"].get("points") or []
    if not points:
        return False
    return datetime.fromisoformat(points[-1]["time"]) <= now - embargo
FALLBACK_TECHS = {"AVNX": "AVNO"}
"""Used for a base only when the preferred tech has no forecast there: the
RAL decks of the western basins carry GFS as ``AVNX`` alone."""
ENSEMBLES: dict[str, tuple[re.Pattern[str], str]] = {
    "gefs": (re.compile(r"^A[CP](\d{2})$"), "AEMN"),
    "ecmwfens": (re.compile(r"^E[CE](\d{2})$"), "EEMN"),
}
_DECK_NAME = re.compile(r"a(al|ep|cp|wp|io|sh)(\d{2})(\d{4})\.dat(?:\.gz)?")


def _wanted_tech(tech: str) -> bool:
    return (
        tech in SINGLE_TECHS
        or tech in FALLBACK_TECHS
        or any(pattern.match(tech) or tech == mean for pattern, mean in ENSEMBLES.values())
    )


def deck_records(text: str, storm: str, origin: str) -> list[dict[str, Any]]:
    """The archive records of one season a-deck, filed under ``storm`` (the
    deck's own id: the ATCF season of its lines is the file's, which a
    line's date does not always give)."""
    lines = [line for line in text.splitlines() if len(line.split(",", 5)) > 5 and _wanted_tech(line.split(",")[4].strip().upper())]
    records_in = [record for record in atcf.parse_atcf("\n".join(lines)) if record.number <= NUMBERED_MAX]
    by_tech: dict[str, list[Forecast]] = {}
    for record_tech in sorted({record.tech for record in records_in}):
        found = atcf.all_forecasts([r for r in records_in if r.tech == record_tech], record_tech)
        by_tech[record_tech] = [forecast for forecasts in found.values() for forecast in forecasts]
    basin = storm[:2]
    out: list[dict[str, Any]] = []
    for tech, (source, kind) in SINGLE_TECHS.items():
        forecasts = {forecast.base: forecast for forecast in by_tech.get(tech, [])}
        for fallback, preferred in FALLBACK_TECHS.items():
            if preferred == tech:
                for forecast in by_tech.get(fallback, []):
                    forecasts.setdefault(forecast.base, forecast)
        for base in sorted(forecasts):
            forecast = forecasts[base]
            if not forecast.points:
                continue
            if kind == "agency":
                forecast = Forecast(base=forecast.base, points=forecast.points)
            out.append(make_record(storm, basin, None, source, kind, origin, forecast.to_json()))
    for source, (pattern, mean_tech) in ENSEMBLES.items():
        members: dict[datetime, dict[int, Forecast]] = {}
        for tech, forecasts in by_tech.items():
            match = pattern.match(tech)
            if match:
                for forecast in forecasts:
                    members.setdefault(forecast.base, {})[int(match.group(1))] = forecast
        means = {forecast.base: forecast for forecast in by_tech.get(mean_tech, [])}
        for base in sorted(set(members) | set(means)):
            member_set = {k: v for k, v in members.get(base, {}).items() if v.points}
            mean = means.get(base)
            if not member_set and (mean is None or not mean.points):
                continue
            leads = tuple(sorted({lead_seconds(p, base) for f in member_set.values() for p in f.points}))
            ensemble = Ensemble(
                base=base,
                leads=leads,
                members=tuple(
                    member_from_points(m, leads, base, {lead_seconds(p, base): p for p in member_set[m].points})
                    for m in sorted(member_set)
                ),
                run=base.strftime("%Y%m%d%H"),
                mean=mean if mean is not None and mean.points else None,
            )
            out.append(make_record(storm, basin, None, source, "ensemble", origin, ensemble.to_json()))
    return out


def _listing(url: str) -> list[str]:
    try:
        return sorted(set(_DECK_NAME.findall(get_ok(url + "/", attempts=3, timeout=120).decode("utf-8", "replace"))))
    except DownloadError as exc:
        if _http_error_code(exc) in (403, 404):
            return []
        raise


def _decks(source: str, season: int) -> list[tuple[str, str]]:
    """(storm id, URL) of every numbered deck of ``season`` the source has."""
    if source == "nhc":
        basins = ("al", "ep", "cp")
        found = [(f, f"{NHC_ARCHIVE_URL}/{season}") for f in _listing(f"{NHC_ARCHIVE_URL}/{season}")]
        if not any(f[0] in basins for f, _ in found):
            found = [(f, NHC_CURRENT_URL) for f in _listing(NHC_CURRENT_URL)]
        gz = True
    else:
        basins = ("wp", "io", "sh")
        found = [(f, f"{UCAR_ADECK_URL}/{season}") for f in _listing(f"{UCAR_ADECK_URL}/{season}")]
        gz = False
    decks = {}
    for (basin, number, year), directory in found:
        if basin not in basins or int(year) != season or int(number) > NUMBERED_MAX:
            continue
        storm = f"{basin.upper()}{number}{year}"
        decks[storm] = f"{directory}/a{basin}{number}{year}.dat" + (".gz" if gz else "")
    return sorted(decks.items())


def _months(first: str, last: str) -> set[str]:
    months = set()
    year, month = int(first[:4]), int(first[5:])
    while f"{year:04d}-{month:02d}" <= last:
        months.add(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _seasons(first: str, last: str) -> list[int]:
    """The seasons whose storms can have forecasts in the months: the
    southern season named after the following year reaches back to July."""
    return list(range(int(first[:4]), int(last[:4]) + 2))


def backfill_decks(archive: Archive, source: str, first: str, last: str) -> dict[str, Any]:
    months = _months(first, last)
    origin = "nhc-atcf" if source == "nhc" else "ucar-atcf"
    decks = 0
    records = 0
    for season in _seasons(first, last):
        for storm, url in _decks(source, season):
            body = get_ok(url, attempts=3, timeout=600)
            text = (gzip.decompress(body) if url.endswith(".gz") else body).decode("utf-8", "replace")
            now = datetime.now(UTC)
            found = [
                r
                for r in deck_records(text, storm, origin)
                if month_of(r["forecast"]["base"]) in months and released(r, now)
            ]
            if found:
                archive.add_records(found)
                decks += 1
                records += len(found)
                LOG.info("tc backfill %s: %s, %d forecasts", source, storm, len(found))
    return {"source": source, "decks": decks, "records": records}


# --- ECMWF on AWS ---------------------------------------------------------


S3_RETRY = {"attempts": 8, "max_elapsed": 600}
"""The bucket answers a burst with ``503 Slow Down``; back off rather than
lose the cycle."""


def _s3_keys(prefix: str) -> list[str]:
    body = get_ok(f"{ECMWF_AWS_URL}/?list-type=2&prefix={prefix}", timeout=120, **S3_RETRY).decode("utf-8")
    return re.findall(r"<Key>([^<]+)</Key>", body)


def ecmwf_cycle_files(cycle: datetime) -> dict[str, str]:
    """``{"ecmwf": key, "ecmwfens": key}`` of a cycle's ``tf`` files on the
    bucket, each present only when it is there."""
    files: dict[str, str] = {}
    for resolution in ECMWF_RESOLUTIONS:
        for source, streams in ECMWF_STREAMS.items():
            for stream in streams:
                keys = _s3_keys(f"{cycle:%Y%m%d}/{cycle:%H}z/{resolution}/{stream}/")
                found = [k for k in keys if k.endswith(f"-{stream}-tf.bufr")]
                if found:
                    files[source] = found[0]
                    break
        if files:
            break
    return files


def ecmwf_tracks_json(path: Path, source: str) -> list[tuple[str, str, str | None, str, dict[str, Any]]]:
    """(key, basin, name, kind, forecast JSON) of every system in one file."""
    kind = "ensemble" if source == "ecmwfens" else "model"
    return [
        (key, basin, storm.name, kind, value.to_json())
        for key, basin, storm, value in ecmwf_tracks(path, ensemble=source == "ecmwfens")
    ]


def ecmwf_records(
    tracks: list[tuple[str, str, str | None, str, dict[str, Any]]], source: str, matcher: BestMatcher
) -> list[dict[str, Any]]:
    """Archive records of one file's systems. A system ECMWF numbered as
    JTWC or NHC does keeps that id; one it numbered itself (the western
    Pacific's, the southern hemisphere's, and every ``7x``) is placed on the season's best-track storm
    its start lies on, or dropped — so a storm's forecasts from before it
    was numbered are kept too."""
    records = []
    for key, basin, name, kind, forecast in tracks:
        storm = key if ATCF_ID.match(key) else None
        if storm is None:
            start = start_position(kind, forecast)
            storm = None if start is None else matcher.match(basin, *start)
        if storm is None:
            continue
        records.append(make_record(storm, storm[:2], name, source, kind, "ecmwf-aws", forecast))
    return records


def _cycles(first: str, last: str) -> Iterator[datetime]:
    cycle = max(datetime(int(first[:4]), int(first[5:]), 1, tzinfo=UTC), ECMWF_FIRST_CYCLE)
    year, month = int(last[:4]), int(last[5:])
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=UTC)
    end = min(end, datetime.now(UTC))
    while cycle < end:
        yield cycle
        cycle += timedelta(hours=6)


def backfill_ecmwf(archive: Archive, first: str, last: str, *, workers: int = 2) -> dict[str, Any]:
    scratch = Path(tempfile.mkdtemp(prefix="tc-ecmwf-"))
    skipped: list[str] = []

    matcher = BestMatcher(archive)

    def one(cycle: datetime) -> list[tuple[str, list[tuple[str, str, str | None, str, dict[str, Any]]]]]:
        records: list[tuple[str, list[tuple[str, str, str | None, str, dict[str, Any]]]]] = []
        try:
            files = ecmwf_cycle_files(cycle)
        except DownloadError as exc:
            LOG.warning("tc backfill ecmwf: cycle %s skipped: %s", f"{cycle:%Y%m%d%H}", exc)
            skipped.append(f"{cycle:%Y%m%d%H}")
            return records
        for source, key in files.items():
            path = scratch / key.replace("/", "_")
            try:
                path.write_bytes(get_ok(f"{ECMWF_AWS_URL}/{key}", timeout=600, **S3_RETRY))
                records.append((source, ecmwf_tracks_json(path, source)))
            except (DownloadError, TcProductError, ValueError) as exc:
                LOG.warning("tc backfill ecmwf: %s skipped: %s", key, exc)
                skipped.append(key)
            finally:
                path.unlink(missing_ok=True)
        return records

    cycles = 0
    records = 0
    try:
        months = sorted(_months(first, last))
        for month in months:
            month_cycles = [c for c in _cycles(month, month)]
            with ThreadPoolExecutor(workers) as pool:
                for files in pool.map(one, month_cycles):
                    for source, tracks in files:
                        for record in ecmwf_records(tracks, source, matcher):
                            try:
                                archive.add_records([record])
                            except TcProductError as exc:
                                LOG.warning("tc backfill ecmwf: %s %s skipped: %s", record["storm"], source, exc)
                                skipped.append(f"{record['storm']} {source} {record['forecast']['base']}")
                                continue
                            records += 1
            cycles += len(month_cycles)
            LOG.info("tc backfill ecmwf: %s, %d cycles, %d forecasts so far", month, len(month_cycles), records)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return {"source": "ecmwf", "cycles": cycles, "records": records, "skipped": sorted(skipped)}


def backfill_ibtracs(archive: Archive, first: str, last: str, *, csv_path: Path | None = None) -> dict[str, Any]:
    """The seasons named by the years of ``first`` and ``last``. ``csv_path``
    is a CSV already on disk: NCEI serves the whole list slowly enough to be
    worth fetching once, and the seasons can then be added a few at a time."""
    first_year, last_year = int(first[:4]), int(last[:4])
    if csv_path is not None:
        text = csv_path.read_text(encoding="utf-8", errors="replace")
    else:
        url = IBTRACS_ALL_URL if first_year < IBTRACS_SINCE1980_FROM else IBTRACS_SINCE1980_URL
        text = get_ok(url, attempts=3, timeout=3600).decode("utf-8", errors="replace")
    seasons = set(range(first_year, last_year + 1))
    # An ATCF id's season can differ from IBTrACS' by a year at the edge of a
    # season, so a neighbour is parsed and the id's own season decides.
    around = seasons | {season + offset for season in seasons for offset in (-1, 1)}
    records = [r for r in ibtracs_best(text, every_storm=True, seasons=around) if int(r["storm"][-4:]) in seasons]
    return {"source": "ibtracs", "storms": add_best_records(archive, records)}


def backfill_curated(archive: Archive, first: str, last: str) -> dict[str, Any]:
    """The curated storms of the seasons named by the years of ``first`` and ``last``."""
    seasons = range(int(first[:4]), int(last[:4]) + 1)
    records = [r for r in json.loads(CURATED_FILE.read_text(encoding="utf-8")) if int(r["storm"][-4:]) in seasons]
    return {"source": "curated", "storms": add_best_records(archive, records)}


# --- the Hong Kong Observatory's last bulletins ----------------------------


HKO_SEQUENCE_MAX = 99
"""A TCID is the season's last two digits and a two-digit sequence number."""


def hko_best_record(product: hko.HkoTrack, matcher: BestMatcher) -> dict[str, Any] | None:
    """The best record one bulletin adds: its past positions under ``hko``,
    on the archived storm most of its synoptic positions lie on (within
    ``MATCH_KM`` of that storm's), or None when no storm is the clear
    choice. The Observatory's number is not an ATCF id, and its name can
    differ from the one the archive carries, so neither is used; a system
    the archive does not hold (never numbered) is not added."""
    track = product.history.to_json()
    votes: Counter[str] = Counter()
    for point in track["points"]:
        storm = matcher.match("WP", point["time"], point["lat"], point["lon"])
        if storm is not None:
            votes[storm] += 1
    ranked = votes.most_common(2)
    if not ranked or (len(ranked) == 2 and ranked[0][1] == ranked[1][1]):
        return None
    storm = ranked[0][0]
    return make_best(storm, None, None, [storm] if ATCF_ID.match(storm) else [], {"hko": track})


def backfill_hko(
    archive: Archive, first: str, last: str, *, get: Callable[[str], bytes | None] | None = None
) -> dict[str, Any]:
    """The seasons named by the years of ``first`` and ``last``. ``get``
    returns a URL's body, or None where nothing is served."""

    def fetch(url: str) -> bytes | None:
        try:
            return get_ok(url, attempts=3, timeout=120)
        except DownloadError as exc:
            if _http_error_code(exc) in (403, 404):
                return None
            raise

    get = get or fetch
    matcher = BestMatcher(archive)
    bulletins = 0
    added: list[str] = []
    unmatched: list[str] = []
    for season in range(int(first[:4]), int(last[:4]) + 1):
        for sequence in range(1, HKO_SEQUENCE_MAX + 1):
            tcid = f"{season % 100:02d}{sequence:02d}"
            payload = get(HKO_TRACK_URL.format(tcid=tcid))
            if payload is None:
                continue
            try:
                product = hko.parse_track(payload)
            except ValueError as exc:
                LOG.warning("tc backfill hko: %s skipped: %s", tcid, exc)
                unmatched.append(tcid)
                continue
            bulletins += 1
            record = hko_best_record(product, matcher)
            if record is None or add_best_records(archive, [record]) == 0:
                unmatched.append(tcid)
                continue
            added.append(f"{tcid} {record['storm']}")
            LOG.info("tc backfill hko: %s on %s", tcid, record["storm"])
    return {"source": "hko", "bulletins": bulletins, "storms": added, "unmatched": unmatched}


RUNNERS: dict[str, Callable[[Archive, str, str], dict[str, Any]]] = {
    "nhc": lambda archive, first, last: backfill_decks(archive, "nhc", first, last),
    "ucar": lambda archive, first, last: backfill_decks(archive, "ucar", first, last),
    "ecmwf": backfill_ecmwf,
    "ibtracs": backfill_ibtracs,
    "curated": backfill_curated,
    "hko": backfill_hko,
}


def backfill(
    archive: Archive, sources: Iterable[str], first: str, last: str, *, ibtracs_csv: Path | None = None
) -> list[dict[str, Any]]:
    return [
        backfill_ibtracs(archive, first, last, csv_path=ibtracs_csv)
        if source == "ibtracs"
        else RUNNERS[source](archive, first, last)
        for source in sources
    ]
