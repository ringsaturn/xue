"""Météo-France's own radiosonde files, the ``meteofrance`` source.

Météo-France publishes every ascent of its fifteen stations — the five in
mainland France and those overseas and in the TAAF — as open data under
the Licence Ouverte 2.0 (data.gouv.fr dataset ``radiosondages``). Its
WIS2 node publishes only the descents, and neither GTS gateway carries the
mainland ascents, so this is where France's soundings come from.

The files sit under ``OBS/RS/<YYYY>/<MM>/<DD>/`` as
``<wmo>.<YYYYMMDDHH>.json``, one per station and batch, behind a plain
HTML directory index that gives each file's size and modification time.
The batches go out twice a day (about 01:40 and 13:40 UTC), and the hour
in a file's name is the batch's, not the ascent's: an 18Z ascent from
Rapa is filed in the next day's ``00`` file. So the nominal time is read
off the launch clock, by the rule the national WIS2 nodes are filed by
(:func:`.bufr.synoptic_hour`).

A file is a JSON array of two objects, the ascent and the descent, an
empty object standing in for one that is missing. Each says which it is
in ``direction`` (``upward`` / ``downward``), but the label is not to be
trusted: of the 49 files of the first two days read, three had the two
swapped, and one held a launch lost at 868 hPa as its ``upward`` and the
relaunch as its ``downward``. So an object is told apart by its own
levels (:func:`climbs`): an ascent is one whose clock runs on as its
pressure falls. Two ascents of one station and hour (a relaunch) are
settled by the product's deduplication, which keeps the longer. The
ascent's ``levels`` are the full-resolution profile — a level every two
seconds, three thousand or more — under named columns, in SI units. They
carry no significance flags, so the thinning keeps the measured level
nearest each standard surface (:data:`.bufr.STANDARD_PRESSURES`) beside
the 3 % rule.
"""

from __future__ import annotations

import html
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable

from . import bufr

SOURCE_ID = "meteofrance"

BASE_URL = "https://files.data.gouv.fr/meteofrance/data/OBS/RS/"

LOOKBACK_HOURS = 48
"""How far back the listing goes: the build's staleness limit. A file is
kept for good, so a watermark never falls behind the listing; the days
listed are the ones this window touches, two or three directory pages."""

FILE_NAME = re.compile(r"^(?P<wmo>\d{5})\.(?P<batch>\d{10})\.json$")

_ROW = re.compile(
    r"<a\s+href=\"[^\"]*\">(?P<name>[^<]+)</a>\s*</td>\s*<td>\s*(?P<size>\d+)\s*</td>\s*<td>\s*(?P<modified>[^<\s]+)\s*</td>",
    re.IGNORECASE,
)

COLUMNS = {
    "p_niv": "pressure",
    "geop": "nonCoordinateGeopotentialHeight",
    "t": "airTemperature",
    "td": "dewpointTemperature",
    "dd": "windDirection",
    "ff": "windSpeed",
}
"""Météo-France's level columns under the BUFR keys :func:`.bufr.assemble`
reads. Their units are the BUFR ones already: pascals, geopotential
metres, kelvin, degrees and metres per second."""

ASCENT = ("upward", "upwards")
"""``direction`` of an ascent: the files say ``upward``, the technical
description ``upwards``. Read only when the levels cannot say
(:func:`climbs`)."""

ELAPSED = "dur_dep"
"""The level column holding the seconds since launch (since the burst, in
a descent)."""


@dataclass(frozen=True)
class ListedFile:
    key: str
    """The path under :data:`BASE_URL`, ``2026/11/11/07145.2026111100.json``."""
    last_modified: datetime
    size: int


def days(now: datetime, hours: int = LOOKBACK_HOURS) -> list[date]:
    """The UTC days the window ending ``now`` touches, oldest first."""
    first = (now - timedelta(hours=hours)).astimezone(UTC).date()
    last = now.astimezone(UTC).date()
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def parse_listing(page: str, prefix: str) -> list[ListedFile]:
    """The files of one day's directory index. A day nothing has been
    filed under yet answers a page with no rows, which is no files."""
    files: list[ListedFile] = []
    for match in _ROW.finditer(page):
        name = html.unescape(match.group("name")).strip()
        if FILE_NAME.match(name) is None:
            continue
        try:
            modified = datetime.fromisoformat(match.group("modified").replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            continue
        files.append(ListedFile(key=prefix + name, last_modified=modified, size=int(match.group("size"))))
    return files


def list_files(
    now: datetime, *, get: Callable[[str], bytes], base_url: str = BASE_URL, hours: int = LOOKBACK_HOURS
) -> list[ListedFile]:
    """Every file under the days the window touches."""
    files: list[ListedFile] = []
    for day in days(now, hours):
        prefix = f"{day:%Y/%m/%d}/"
        files.extend(parse_listing(get(base_url + prefix).decode("utf-8", errors="replace"), prefix))
    return files


@dataclass(frozen=True)
class FileResult:
    soundings: tuple[bufr.Sounding, ...]
    ascents: int
    """Ascent objects the file held."""
    descents: int
    """Descent objects the file held, whatever their label, left out like
    a BUFR descent."""
    dropped: int
    """Ascents with no usable station, position or level."""


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return float(value)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _batch(name: str) -> datetime | None:
    match = FILE_NAME.match(name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group("batch"), "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError:
        return None


def parse_file(data: bytes, *, name: str, arrived: datetime) -> FileResult:
    """The soundings of one file. Raises ``ValueError`` (``json`` 's
    included) for a file that is not the documented array."""
    document = json.loads(data)
    if isinstance(document, dict):
        document = [document]
    if not isinstance(document, list):
        raise ValueError(f"{name}: not a JSON array")
    soundings: list[bufr.Sounding] = []
    ascents = descents = dropped = 0
    for item in document:
        if not isinstance(item, dict) or not item:
            continue
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError(f"{name}: an object with no metadata")
        if not climbs(item, metadata):
            descents += 1
            continue
        ascents += 1
        sounding = _ascent(item, metadata, name=name, arrived=arrived)
        if sounding is None:
            dropped += 1
            continue
        soundings.append(sounding)
    return FileResult(tuple(soundings), ascents, descents, dropped)


def _column(levels: Any, name: str) -> int | None:
    if not isinstance(levels, dict):
        return None
    for column in levels.get("properties") or []:
        if isinstance(column, dict) and column.get("name") == name and isinstance(column.get("index"), int):
            return column["index"]
    return None


def climbs(item: dict[str, Any], metadata: dict[str, Any]) -> bool:
    """Whether an object is an ascent, by its levels: the level at the
    lowest pressure was measured later than the level at the highest.
    A descent runs the other way, its clock starting at the burst. An
    object whose levels cannot say — one level, no clock column — falls
    back on its ``direction``."""
    levels = item.get("levels")
    pressure, elapsed = _column(levels, "p_niv"), _column(levels, ELAPSED)
    if pressure is not None and elapsed is not None:
        timed = [
            (_number(row[pressure]), _number(row[elapsed]))
            for row in levels.get("observations") or []
            if isinstance(row, list) and len(row) > max(pressure, elapsed)
        ]
        timed = [(p, t) for p, t in timed if p is not None and t is not None]
        if len(timed) >= 2:
            bottom, top = max(timed), min(timed)
            if bottom[0] != top[0] and bottom[1] != top[1]:
                return top[1] > bottom[1]
    return metadata.get("direction") in ASCENT


def _ascent(item: dict[str, Any], metadata: dict[str, Any], *, name: str, arrived: datetime) -> bufr.Sounding | None:
    station = metadata.get("station")
    if not isinstance(station, dict):
        return None
    wmo = str(station.get("geo_id_wmo") or "").strip()
    if not re.fullmatch(r"\d{5}", wmo) or wmo.startswith("00"):
        return None
    lat = _number(station.get("lat"))
    lon = _number(station.get("lon"))
    if lat is None or lon is None or not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
        return None
    clock = _time(metadata.get("date"))
    # The launch clock files the ascent; a file without one falls back on
    # its batch hour, which is the nominal hour of every twice-daily site.
    nominal = bufr.synoptic_hour(clock) if clock is not None else _batch(name)
    if nominal is None:
        return None
    levels = item.get("levels")
    if not isinstance(levels, dict):
        return None
    columns: dict[str, int] = {}
    for column in levels.get("properties") or []:
        if isinstance(column, dict) and column.get("name") in COLUMNS and isinstance(column.get("index"), int):
            columns[COLUMNS[column["name"]]] = column["index"]
    if "pressure" not in columns:
        return None
    rows = [
        {key: observation[index] if index < len(observation) else None for key, index in columns.items()}
        for observation in levels.get("observations") or []
        if isinstance(observation, list)
    ]
    if len(rows) < 2:
        # A level or none: what is left of a profile whose signal was
        # lost, no sounding to publish.
        return None
    elev = _number(metadata.get("ht_sta_mer"))
    if elev is None:
        elev = _number(station.get("alti"))
    sonde = _number(metadata.get("rs_type"))
    return bufr.assemble(
        rows,
        key=wmo,
        wigos=None,
        wmo=wmo,
        time=nominal,
        launched=bufr.plausible_launch(clock, nominal),
        lat=lat,
        lon=lon,
        elev=elev,
        sonde=None if sonde is None else int(sonde),
        name=bufr.BulletinName(header=name, nominal=nominal, arrived=arrived.astimezone(UTC), correction=None),
        gateway=SOURCE_ID,
        anchors=bufr.STANDARD_PRESSURES,
    )
