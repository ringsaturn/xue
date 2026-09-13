"""``tc-build``: the fetched directories of one issue hour → one immutable
product directory and the live pointer. The only module where the sources
meet.

Each source is parsed into *sightings* (:class:`identity.Sighting`) —
one per system it knows about, carrying what it says about it — the
identity rules group them into systems, and each system is folded into a
storm file. A source that failed to fetch, or whose file fails to parse,
is recorded in ``sources`` with its error and contributes nothing;
the pointer is withheld only when no agency and no model contributed at
all, since a product with nothing in it would take an empty map live.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..errors import TcProductError, XueError
from . import atcf, bufrtracks, ibtracs, tcw
from .fetch import SOURCE_IDS, FetchResult, issue_raw_directory, read_fetch_record
from .identity import PreviousSystem, Sighting, System, resolve, sighting_kind
from .registry import MODELS, basin_of_letter
from .schema import (
    INDEX_FILENAME,
    POINTER_FILENAME,
    SCHEMA_VERSION,
    build_pointer,
    crc32_hex,
    encode_json,
    issue_directory,
    read_index,
    validate_index,
    validate_storm,
    write_bytes_atomic,
)
from .track import Ensemble, Forecast, Point, SourceStatus, Track, iso_z, lead_seconds, member_from_points

LOG = logging.getLogger(__name__)

STALE_HOURS = 48
"""A system none of the sources has placed within this long of the issue
hour is over — IBTrACS keeps a storm in its active file for days after
the last advisory."""

# ``best`` keys are the IBTrACS agency keys; the working best tracks of
# the two US centres both land under ``usa``, the fresher file first.
_BEST_PRIORITY = {"nhc": 0, "jtwc": 1, "ibtracs": 2}
_TCW_NAME = re.compile(r"^([a-z]{2})(\d{2})(\d{2})\.tcw$")
_NHC_DECK = re.compile(r"^([ab])([a-z]{2}\d{6})\.dat$")
_GEFS_MEMBER = re.compile(r"^(ac00|aemn|ap\d{2})\.t\d{2}z\.cyclone\.trackatcfunix$")


@dataclass(frozen=True)
class Contribution:
    kind: str
    """``agency`` / ``best`` / ``model`` / ``ensemble`` / ``alert`` / ``sid``."""
    key: str
    value: Any
    source: str


def _sighting(
    source: str,
    key: str,
    basin: str,
    point: Point,
    contributions: list[Contribution],
    *,
    name: str | None = None,
    first_seen: datetime | None = None,
) -> Sighting:
    return Sighting(
        source=source,
        key=key,
        kind=sighting_kind(key),
        basin=basin,
        time=point.time,
        lat=point.lat,
        lon=point.lon,
        name=name,
        first_seen=first_seen,
        payload=contributions,
    )


def _atcf_key(basin: str, number: int, season: int) -> str:
    """The identity key of an ATCF system: the full id when numbered, the
    reusable short id for an invest."""
    return f"{basin}{number:02d}{season}" if number <= 49 else f"{basin}{number:02d}"


# --- per-source parsing ------------------------------------------------


def _parse_jtwc(directory: Path, files: list[str]) -> list[Sighting]:
    sightings = []
    for name in files:
        match = _TCW_NAME.match(name)
        if not match:
            continue
        basin, number, yy = match.group(1).upper(), int(match.group(2)), int(match.group(3))
        product = tcw.parse_tcw((directory / name).read_text(encoding="utf-8", errors="replace"))
        season = product.base.year if product.base.year % 100 == yy else 2000 + yy
        key = _atcf_key(basin, number, season)
        history = Track(points=product.history.points, provisional=True, source="jtwc")
        first_seen = history.points[0].time if history.points else None
        contributions = [Contribution("best", "usa", history, "jtwc")]
        if product.forecast is not None and product.forecast.points:
            contributions.append(Contribution("agency", "jtwc", product.forecast, "jtwc"))
            anchor = product.forecast.points[0]
        elif history.points:
            anchor = history.points[-1]
        else:
            continue
        if product.kind == "alert":
            contributions.append(
                Contribution(
                    "alert",
                    "jtwc",
                    {
                        "time": iso_z(product.base),
                        "line": None if product.alert_line is None else [list(end) for end in product.alert_line],
                        "halfWidth": product.alert_half_width,
                        "center": None if product.alert_center is None else list(product.alert_center),
                    },
                    "jtwc",
                )
            )
        sightings.append(_sighting("jtwc", key, basin, anchor, contributions, name=product.name, first_seen=first_seen))
    return sightings


def _parse_nhc(directory: Path, files: list[str]) -> list[Sighting]:
    names: dict[str, str] = {}
    current = directory / "CurrentStorms.json"
    if current.exists():
        try:
            for storm in json.loads(current.read_text(encoding="utf-8")).get("activeStorms", []):
                storm_id = str(storm.get("id", "")).upper()
                if storm.get("name"):
                    names[storm_id] = str(storm["name"]).upper()
        except (json.JSONDecodeError, AttributeError):
            pass
    decks: dict[str, dict[str, list[atcf.AtcfRecord]]] = {}
    for name in files:
        match = _NHC_DECK.match(name)
        if not match:
            continue
        storm_id = match.group(2).upper()
        decks.setdefault(storm_id, {})[match.group(1)] = atcf.parse_atcf(
            (directory / name).read_text(encoding="utf-8", errors="replace")
        )
    sightings = []
    for storm_id, deck in decks.items():
        basin, number, season = storm_id[:2], int(storm_id[2:4]), int(storm_id[4:])
        key = _atcf_key(basin, number, season)
        contributions = []
        anchor: Point | None = None
        name = names.get(storm_id)
        first_seen = None
        if "b" in deck:
            track = atcf.best_track(deck["b"], storm_id)
            if track is not None and track.points:
                track = Track(points=track.points, provisional=True, source="nhc")
                contributions.append(Contribution("best", "usa", track, "nhc"))
                anchor = track.points[-1]
                first_seen = track.points[0].time
                name = name or atcf.storm_name(deck["b"], storm_id)
        if "a" in deck:
            official = atcf.forecasts(deck["a"], "OFCL", storm_id=storm_id).get(storm_id)
            if official is not None and official.points:
                contributions.append(
                    Contribution("agency", "nhc", Forecast(base=official.base, points=official.points), "nhc")
                )
                anchor = anchor or official.points[0]
        if anchor is None:
            continue
        sightings.append(_sighting("nhc", key, basin, anchor, contributions, name=name, first_seen=first_seen))
    return sightings


def _parse_gfs(directory: Path, files: list[str]) -> list[Sighting]:
    sightings = []
    for name in files:
        records = atcf.parse_atcf((directory / name).read_text(encoding="utf-8", errors="replace"))
        for storm_id, forecast in atcf.forecasts(records, "AVNO").items():
            if not forecast.points:
                continue
            basin, number, season = storm_id[:2], int(storm_id[2:4]), int(storm_id[4:])
            sightings.append(
                _sighting(
                    "gfs",
                    _atcf_key(basin, number, season),
                    basin,
                    forecast.points[0],
                    [Contribution("model", "gfs", forecast, "gfs")],
                    first_seen=forecast.base,
                )
            )
    return sightings


def _parse_gefs(directory: Path, files: list[str]) -> list[Sighting]:
    members: dict[str, dict[int, Forecast]] = {}
    means: dict[str, Forecast] = {}
    for name in files:
        match = _GEFS_MEMBER.match(Path(name).name)
        if not match:
            continue
        tech = match.group(1).upper()
        records = atcf.parse_atcf((directory / name).read_text(encoding="utf-8", errors="replace"))
        for storm_id, forecast in atcf.forecasts(records, tech).items():
            if tech == "AEMN":
                means[storm_id] = forecast
            else:
                members.setdefault(storm_id, {})[0 if tech == "AC00" else int(tech[2:])] = forecast
    sightings = []
    for storm_id in sorted(set(members) | set(means)):
        member_set = members.get(storm_id, {})
        mean = means.get(storm_id)
        bases = {f.base for f in member_set.values()} | ({mean.base} if mean else set())
        if not bases:
            continue
        base = max(bases)
        member_set = {k: v for k, v in member_set.items() if v.base == base}
        if mean is not None and mean.base != base:
            mean = None
        leads = tuple(sorted({lead_seconds(p, base) for f in member_set.values() for p in f.points}))
        ensemble = Ensemble(
            base=base,
            leads=leads,
            members=tuple(
                member_from_points(
                    member_id, leads, base, {lead_seconds(p, base): p for p in member_set[member_id].points}
                )
                for member_id in sorted(member_set)
            ),
            run=base.strftime("%Y%m%d%H"),
            mean=mean,
        )
        anchor = (
            mean.points[0]
            if mean is not None and mean.points
            else next((f.points[0] for f in member_set.values() if f.points), None)
        )
        if anchor is None:
            continue
        basin, number, season = storm_id[:2], int(storm_id[2:4]), int(storm_id[4:])
        sightings.append(
            _sighting(
                "gefs",
                _atcf_key(basin, number, season),
                basin,
                anchor,
                [Contribution("ensemble", "gefs", ensemble, "gefs")],
                first_seen=base,
            )
        )
    return sightings


def _ecmwf_key(identifier: str, base: datetime) -> tuple[str, str] | None:
    """(identity key, basin) of a ``stormIdentifier``: ``14E`` is the
    numbered ``EP142026``, ``70W`` stays the model's own name."""
    if len(identifier) < 2:
        return None
    basin = basin_of_letter(identifier[-1])
    digits = identifier[:-1]
    if basin is None or not digits.isdigit():
        return None
    number = int(digits)
    if number <= 49:
        return _atcf_key(basin, number, base.year), basin
    return identifier, basin


def _parse_ecmwf(directory: Path, files: list[str], *, ensemble: bool) -> list[Sighting]:
    from ..eccodescli import bufr_dump_json  # eccodes is only needed on this path

    sightings = []
    source = "ecmwfens" if ensemble else "ecmwf"
    for name in files:
        for storm in bufrtracks.parse_bufr_tracks(bufr_dump_json(directory / name)):
            resolved = _ecmwf_key(storm.identifier, storm.base)
            if resolved is None or not storm.members:
                continue
            key, basin = resolved
            if ensemble:
                leads = tuple(sorted({lead_seconds(p, storm.base) for m in storm.members for p in m.forecast.points}))
                value: Forecast | Ensemble = Ensemble(
                    base=storm.base,
                    leads=leads,
                    members=tuple(
                        member_from_points(
                            m.member, leads, storm.base, {lead_seconds(p, storm.base): p for p in m.forecast.points}
                        )
                        for m in sorted(storm.members, key=lambda m: m.member)
                    ),
                    run=storm.base.strftime("%Y%m%d%H"),
                )
                kind = "ensemble"
            else:
                value = storm.members[0].forecast
                kind = "model"
            anchor = next((m.forecast.points[0] for m in storm.members if m.forecast.points), None)
            if anchor is None:
                continue
            # A model's own system is named after the run that found it,
            # not after the hour it first appears in the forecast.
            sightings.append(
                _sighting(
                    source,
                    key,
                    basin,
                    anchor,
                    [Contribution(kind, source, value, source)],
                    name=storm.name,
                    first_seen=storm.base,
                )
            )
    return sightings


def _parse_ibtracs(directory: Path, files: list[str]) -> list[Sighting]:
    sightings = []
    for name in files:
        storms = ibtracs.parse_ibtracs((directory / name).read_text(encoding="utf-8", errors="replace"))
        for storm in storms.values():
            if storm.atcf_id is None or "usa" not in storm.agencies:
                continue
            usa = storm.agencies["usa"]
            if not usa.points:
                continue
            contributions = [Contribution("sid", storm.sid, storm.sid, "ibtracs")]
            for agency, track in storm.agencies.items():
                contributions.append(
                    Contribution(
                        "best",
                        agency,
                        Track(points=track.points, provisional=track.provisional, source="ibtracs"),
                        "ibtracs",
                    )
                )
            basin, number, season = storm.atcf_id[:2], int(storm.atcf_id[2:4]), int(storm.atcf_id[4:])
            sightings.append(
                _sighting(
                    "ibtracs",
                    _atcf_key(basin, number, season),
                    basin,
                    usa.points[-1],
                    contributions,
                    name=storm.name,
                    first_seen=usa.points[0].time,
                )
            )
    return sightings


_PARSERS = {
    "jtwc": _parse_jtwc,
    "nhc": _parse_nhc,
    "gfs": _parse_gfs,
    "gefs": _parse_gefs,
    "ecmwf": lambda directory, files: _parse_ecmwf(directory, files, ensemble=False),
    "ecmwfens": lambda directory, files: _parse_ecmwf(directory, files, ensemble=True),
    "ibtracs": _parse_ibtracs,
}


# --- folding systems into storm files ----------------------------------


def _fold(system: System, sources: list[SourceStatus]) -> dict[str, Any]:
    best: dict[str, tuple[int, Track]] = {}
    agencies: dict[str, Forecast] = {}
    models: dict[str, Forecast | Ensemble] = {}
    alert: dict[str, Any] | None = None
    sid: str | None = None
    for sighting in system.sightings:
        for contribution in sighting.payload or []:
            if contribution.kind == "best":
                rank = _BEST_PRIORITY.get(contribution.source, 9)
                current = best.get(contribution.key)
                if current is None or rank < current[0]:
                    best[contribution.key] = (rank, contribution.value)
            elif contribution.kind == "agency":
                current_forecast = agencies.get(contribution.key)
                if current_forecast is None or contribution.value.base > current_forecast.base:
                    agencies[contribution.key] = contribution.value
            elif contribution.kind in ("model", "ensemble"):
                current_model = models.get(contribution.key)
                if current_model is None or contribution.value.base > current_model.base:
                    models[contribution.key] = contribution.value
            elif contribution.kind == "alert":
                alert = contribution.value
            elif contribution.kind == "sid":
                sid = contribution.value
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "id": system.id,
        "level": system.level,
        "basin": system.basin,
        "name": system.name,
        "sid": sid,
        "intl": None,
        "aliases": system.aliases,
        "best": {key: track.to_json() for key, (_, track) in sorted(best.items())},
        "agencies": {key: forecast.to_json() for key, forecast in sorted(agencies.items())},
        "models": {
            key: models[key].to_json()
            for key in sorted(models, key=lambda k: list(MODELS).index(k) if k in MODELS else 99)
        },
        "impact": {},
        "alert": alert,
        "sources": [status.to_json() for status in sources],
    }
    validate_storm(payload)
    return payload


def _headline(storm: dict[str, Any]) -> dict[str, Any]:
    """The index entry's summary: the latest observed position and
    intensity, from the best track when there is one, else from the
    first point of an agency forecast, else of a model's."""
    point: dict[str, Any] | None = None
    agency: str | None = None
    for key in ("usa", *sorted(storm["best"])):
        track = storm["best"].get(key)
        if track and track["points"]:
            point, agency = track["points"][-1], track["source"]
            break
    if point is None:
        for key, forecast in storm["agencies"].items():
            if forecast["points"]:
                point, agency = forecast["points"][0], key
                break
    if point is None:
        for key, model in storm["models"].items():
            points = model.get("points") or (model.get("mean") or {}).get("points")
            if points:
                point, agency = points[0], key
                break
    if point is None:
        return {"position": None, "vmax": None, "pmin": None, "class": None, "classAgency": None}
    return {
        "position": {"time": point["time"], "lat": point["lat"], "lon": point["lon"]},
        "vmax": point.get("vmax"),
        "pmin": point.get("pmin"),
        "class": point.get("class"),
        "classAgency": agency,
    }


def previous_systems(index: dict[str, Any]) -> list[PreviousSystem]:
    systems = []
    for entry in index.get("storms", []):
        position = entry.get("position")
        if not position:
            continue
        systems.append(
            PreviousSystem(
                id=entry["id"],
                level=entry["level"],
                basin=entry["basin"],
                aliases=dict(entry.get("aliases", {})),
                last_time=datetime.fromisoformat(position["time"]),
                last_lat=float(position["lat"]),
                last_lon=float(position["lon"]),
                name=entry.get("name"),
            )
        )
    return systems


def build_product(
    issue: datetime,
    raw_root: Path,
    output_root: Path,
    *,
    sources: tuple[str, ...] = SOURCE_IDS,
    previous_index: dict[str, Any] | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read the issue's fetched directories, write ``tc.<issue>/`` under
    ``output_root`` and, when anything contributed, the pointer."""
    now = now or datetime.now(UTC)
    raw_directory = issue_raw_directory(raw_root, issue)
    directory = output_root / issue_directory(issue)
    if (directory / INDEX_FILENAME).exists() and not force:
        raise TcProductError(f"{directory / INDEX_FILENAME} exists; pass --force to rebuild the issue")
    statuses: list[SourceStatus] = []
    sightings: list[Sighting] = []
    for source_id in sources:
        if source_id not in _PARSERS:
            raise XueError(f"unknown tc source {source_id!r}; choose from {', '.join(SOURCE_IDS)}")
        result: FetchResult | None = read_fetch_record(raw_directory, source_id)
        if result is None:
            statuses.append(SourceStatus(source_id, False, error="not fetched"))
            continue
        status = result.status
        if status.ok:
            try:
                found = _PARSERS[source_id](raw_directory / source_id, result.files)
            except (XueError, ValueError, OSError, KeyError) as exc:
                LOG.warning("tc %s: parse failed: %s", source_id, exc)
                status = SourceStatus(
                    source_id, False, fetched=status.fetched, url=status.url, cycle=status.cycle, error=f"parse: {exc}"
                )
            else:
                sightings.extend(found)
                status.detail["systems"] = len(found)
        statuses.append(status)

    previous = previous_systems(previous_index) if previous_index else []
    resolution = resolve(sightings, previous, issue)
    stale_before = issue - timedelta(hours=STALE_HOURS)
    systems = [
        system
        for system in resolution.systems
        if system.sightings and max(s.time for s in system.sightings) >= stale_before
    ]
    systems.sort(key=lambda system: (system.level, system.basin, system.id))

    entries = []
    for system in systems:
        storm = _fold(system, statuses)
        payload = encode_json(storm)
        path = f"{system.id}.json"
        write_bytes_atomic(directory / path, payload)
        entry = {
            "id": system.id,
            "level": system.level,
            "basin": system.basin,
            "name": system.name,
            "path": path,
            "byteLength": len(payload),
            "crc32": crc32_hex(payload),
            "aliases": system.aliases,
            **_headline(storm),
            "agencies": sorted(storm["agencies"]),
            "models": sorted(storm["models"], key=lambda k: list(MODELS).index(k) if k in MODELS else 99),
            "best": sorted(storm["best"]),
        }
        entries.append(entry)

    ids = {entry["id"] for entry in entries}
    crosswalk = {old: new for old, new in resolution.crosswalk.items() if new in ids}
    if previous_index:
        for old, new in previous_index.get("crosswalk", {}).items():
            new = crosswalk.get(new, new)
            if new in ids and old not in ids:
                crosswalk.setdefault(old, new)
    index = {
        "schemaVersion": SCHEMA_VERSION,
        "issued": iso_z(issue),
        "generated": iso_z(now),
        "storms": entries,
        "crosswalk": crosswalk,
        "sources": [status.to_json() for status in statuses],
    }
    validate_index(index)
    index_bytes = encode_json(index)
    write_bytes_atomic(directory / INDEX_FILENAME, index_bytes)

    contributed = any(status.ok for status in statuses if status.id != "ibtracs")
    pointer_path: Path | None = None
    if contributed:
        pointer = build_pointer(issue, f"{issue_directory(issue)}/{INDEX_FILENAME}", index_bytes)
        pointer_path = output_root / POINTER_FILENAME
        write_bytes_atomic(pointer_path, encode_json(pointer))
    else:
        LOG.warning("tc: no agency or model contributed; the pointer is not written")
    return {
        "issue": issue.strftime("%Y%m%d%H"),
        "directory": str(directory),
        "storms": [{"id": e["id"], "level": e["level"], "name": e["name"], "basin": e["basin"]} for e in entries],
        "crosswalk": crosswalk,
        "sources": index["sources"],
        "pointer": None if pointer_path is None else str(pointer_path),
    }


def load_previous_index(path: Path | None, output_root: Path) -> dict[str, Any] | None:
    """The previous hour's index: the one given, else the one the local
    pointer names, else nothing (a first build, or a fresh checkout)."""
    if path is not None:
        return read_index(path)
    pointer = output_root / POINTER_FILENAME
    if not pointer.exists():
        return None
    try:
        named = json.loads(pointer.read_text(encoding="utf-8")).get("path")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(named, str):
        return None
    index_path = output_root / named
    if not index_path.exists():
        return None
    try:
        return read_index(index_path)
    except TcProductError as exc:
        LOG.warning("tc: ignoring the previous index at %s: %s", index_path, exc)
        return None
