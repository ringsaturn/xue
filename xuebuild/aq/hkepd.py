"""The ``hkepd`` network: the Hong Kong Environmental Protection
Department's air quality monitoring network, 15 general and 3 roadside
stations reporting hourly means.

One file carries the whole network's last 24 hours:
``https://www.aqhi.gov.hk/epd/ddata/html/out/24pc_Eng.xml``, a
``PollutantConcentration`` element per station and hour with the station's
English name, an RFC 2822 ``DateTime`` in Hong Kong time and the six
pollutants, all in µg/m³ (CO included), ``-`` where there is no value. The
file is rewritten about ten minutes after each hour, and its newest
``DateTime`` is then that hour — the hour that has just ended, since the
mean of the hour beginning then does not exist yet — so ``DateTime`` is the
end of the averaging hour and is published as it stands. ``lastBuildDate``
runs half an hour ahead of the file and is not an observation time.

Since every round reads the full day again, a missed round costs nothing.
The gases are turned back into mixing ratios at the 20 °C, 1 atm reference
(:func:`~.schema.to_mixing_ratio`); the particles stay in µg/m³.

The file carries no positions. They come from the committed table
``hkepd_stations.json``, recorded once from the network's CSDI WFS layer
(``csdi:AQMN``) and keyed by the XML's station names; a name the table does
not know is counted and dropped.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import xml.etree.ElementTree as ElementTree
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from ..airport.fetch import SourceStatus
from ..errors import DownloadError, XueError
from ..pointproduct import write_bytes_atomic
from .http import get
from .schema import AqProductError, bounded, round_directory, to_mixing_ratio
from .station import NetworkRead, Observation, StationMeta

LOG = logging.getLogger(__name__)

NETWORK = "hkepd"
SOURCE = "epd-24pc"
URL = "https://www.aqhi.gov.hk/epd/ddata/html/out/24pc_Eng.xml"
FILENAME = "24pc_Eng.xml"
TABLE = Path(__file__).with_name("hkepd_stations.json")
AGENCY = "EPD"

COLUMNS = {"pm25": "PM2.5", "pm10": "PM10", "o3": "O3", "no2": "NO2", "so2": "SO2", "co": "CO"}
GASES = frozenset({"o3", "no2", "so2", "co"})


def network_directory(raw_root: Path, moment: datetime) -> Path:
    return raw_root / round_directory(moment) / NETWORK


def load_table(path: Path = TABLE) -> dict[str, StationMeta]:
    """The committed station table → stations by the XML's English name."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        entry["name"]: StationMeta(
            id=f"{NETWORK}:{entry['slug']}",
            name=entry["name"],
            names=entry.get("names") or None,
            lat=float(entry["lat"]),
            lon=float(entry["lon"]),
            elev=entry.get("elev"),
            rank=int(entry["rank"]),
            agency=AGENCY,
        )
        for entry in payload["stations"]
    }


# --- fetch -------------------------------------------------------------


def fetch(
    raw_root: Path, moment: datetime, since: datetime | None, *, force: bool = False, now: datetime | None = None
) -> list[SourceStatus]:
    """Fetch the 24-hour file into ``aq.<round>/hkepd/``. ``since`` is not
    needed: the file always holds the whole window."""
    del since
    now = now or datetime.now(UTC)
    path = network_directory(raw_root, moment) / FILENAME
    relative = f"{round_directory(moment)}/{NETWORK}/{FILENAME}"
    if path.exists() and not force:
        return [SourceStatus(SOURCE, True, fetched=now, url=URL, file=relative)]
    try:
        response = get(URL)
        if response.body is None:
            raise DownloadError(f"{URL} is gone (HTTP 404)")
    except (XueError, urllib.error.URLError, OSError, ValueError) as exc:
        LOG.warning("hkepd: %s", exc)
        return [SourceStatus(SOURCE, False, fetched=now, url=URL, error=str(exc))]
    write_bytes_atomic(path, response.body)
    return [SourceStatus(SOURCE, True, fetched=now, url=URL, file=relative)]


# --- read --------------------------------------------------------------


def _value(key: str, text: str | None) -> float | None:
    if text is None or not text.strip() or text.strip() == "-":
        return None
    try:
        micrograms = float(text)
    except ValueError:
        return None
    if micrograms != micrograms:
        return None
    return bounded(key, to_mixing_ratio(key, micrograms) if key in GASES else micrograms)


def parse_file(data: bytes) -> list[tuple[str, int, dict[str, float | None]]]:
    """``24pc_Eng.xml`` → ``(station name, epoch at the end of the hour,
    elements)`` per element."""
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise AqProductError(f"24pc_Eng.xml is not XML: {exc}") from exc
    rows: list[tuple[str, int, dict[str, float | None]]] = []
    for element in root.iter("PollutantConcentration"):
        name = (element.findtext("StationName") or "").strip()
        stamp = (element.findtext("DateTime") or "").strip()
        try:
            moment = parsedate_to_datetime(stamp)
        except (TypeError, ValueError):
            LOG.warning("hkepd: %s has no usable DateTime %r; skipping it", name or "a row", stamp)
            continue
        if moment.tzinfo is None:
            continue
        elements = {key: _value(key, element.findtext(column)) for key, column in COLUMNS.items()}
        rows.append((name, int(moment.astimezone(UTC).timestamp()), elements))
    return rows


def read(raw_root: Path, statuses: dict[str, SourceStatus]) -> NetworkRead:
    status = statuses.get(SOURCE)
    if status is None or not status.ok or not status.file:
        return NetworkRead({}, [], False)
    table = load_table()
    rows = parse_file((raw_root / status.file).read_bytes())
    observations: list[Observation] = []
    stations: dict[str, StationMeta] = {}
    unplaced: set[str] = set()
    for name, epoch, elements in rows:
        meta = table.get(name)
        if meta is None:
            unplaced.add(name)
            continue
        stations[meta.id] = meta
        observations.append((meta.id, epoch, elements))
    if unplaced:
        LOG.warning("hkepd: stations not in the table, dropped: %s", ", ".join(sorted(unplaced)))
    status.detail["reports"] = len(observations)
    status.detail["unplaced"] = len(unplaced)
    return NetworkRead(stations, observations, True)
