"""TEMP BUFR → :class:`Sounding`, one per subset.

The bytes are read through ``bufr_dump -j f`` (eccodes), the same flat
``{key, value, index, …}`` stream ``xuebuild.tc.bufrtracks`` walks. A
TEMP bulletin is one or more messages, each with one or more subsets, one
subset per station; ``subsetNumber`` marks the start of each in the
uncompressed case (all three sample bulletins, and every one seen on the
gateways), and a compressed message instead carries one array per key with
a value per subset. Both are handled the same way: the stream is cut into
subsets and each subset is walked as a flat list of ``(key, value)``.

Within a subset the header names the station (WIGOS identifier and / or
the WMO block and station number), its position and height, the
radiosonde type and the launch clock; then the level replication repeats,
per level, ``timePeriod``, ``extendedVerticalSoundingSignificance``,
``pressure``, ``nonCoordinateGeopotentialHeight``, the two displacements,
``airTemperature``, ``dewpointTemperature``, ``windDirection`` and
``windSpeed``. The template's order is not relied on: a level ends when a
level key arrives that the level being filled already holds, which is true
whichever of them the template puts first.

Everything is turned into the product's fixed point here (``docs/sounding.md``
§2) so nothing downstream sees a float from a file.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

MISSING = -32768
"""The missing value of every fixed-point level array."""

BUFR_MAGIC = b"BUFR"
"""What a BUFR message starts with, after the GTS text header the
gateways leave in front of it (``IUSC01 RJTD 140000\\r\\r\\n``). An object
without it somewhere near the front is not BUFR at all: the gateways
republish the GTS stream as it is, and a station with nothing to report
sends a plain-text NIL bulletin of a few dozen bytes. Those are normal
traffic, not corruption, and are counted apart from what fails to
decode."""

VALUE_BOUNDS = {
    "p": (1, 120000),
    "z": (-1000, 100000),
    "t": (10000, 40000),
    "td": (10000, 40000),
    "wd": (0, 360),
    "ws": (0, 3000),
    "sig": (0, 262143),
}
"""The fixed-point range each level array stays inside — pressure in
pascals, geopotential height in metres, temperature and dew point in
centikelvin, direction in degrees, speed in decimetres per second, and the
18-bit significance flags.

The parser enforces these, so a value it emits is always one the validator
admits. Live GTS data needs it: bulletins carry wind directions of 504°
and the like, and one such value must not take down the hour's other
seven hundred stations. A value outside its range is not a measurement, so
it becomes ``MISSING``; a *pressure* outside its range costs the level,
since pressure is the vertical axis and a level without one has no place
on it. ``schema.py`` validates against this same table."""

TROPOPAUSE_BIT = 1 << 15
"""``extendedVerticalSoundingSignificance`` (BUFR flag table 0 08 042) is
an 18-bit field numbered left to right, so bit 1 (surface) is 1 << 17 and
bit 3 (tropopause level) is 1 << 15. The sample bulletins agree: the
levels carrying 32768 alone sit at 109–164 hPa."""

SURFACE_BIT = 1 << 17
STANDARD_BIT = 1 << 16

LEVEL_KEYS = frozenset(
    {
        "timePeriod",
        "extendedVerticalSoundingSignificance",
        "pressure",
        "nonCoordinateGeopotentialHeight",
        "latitudeDisplacement",
        "longitudeDisplacement",
        "airTemperature",
        "dewpointTemperature",
        "windDirection",
        "windSpeed",
    }
)
"""The keys the level replication repeats. A level is opened by the first
of them and closed by the first repeat of one already set."""

# A_<TTAAii><CCCC><ddhhmm>[BBB]_C_<gateway>_<yyyymmddHHMMSS>_<n>[.bufr] —
# the WMO file naming convention as the GTS→WIS2 gateways write it.
# ``BBB`` is the bulletin's correction / delayed / amended suffix (``CCA``,
# ``RRA``, ``CCB``…). The extension is optional because the two gateways
# differ over it: the JMA one writes ``.bufr``, the DWD one stops at the
# sequence number.
FILE_NAME = re.compile(
    r"^A_(?P<designator>[A-Z]{4}\d{2})(?P<centre>[A-Z]{4})(?P<ddhhmm>\d{6})(?P<bbb>[A-Z]{3})?"
    r"_C_(?P<gateway_centre>[A-Z]{4})_(?P<arrived>\d{14})_(?P<sequence>\d+)(?:\.(?:bufr|bin))?$"
)

WIGOS_LOCAL = re.compile(r"^[0-9A-Za-z_]+$")
"""A WIGOS local identifier the product can use as a file name. The
identifier is a character string in BUFR; one carrying anything else
falls back to the legacy form (``schema.STATION_ID``)."""


@dataclass(frozen=True)
class BulletinName:
    """What the gateway's file name says about a bulletin."""

    header: str
    """``IUSC01 RJTD 140000``, plus `` CCA`` when the name carries a
    correction suffix — the WMO abbreviated heading, verbatim."""
    nominal: datetime
    """The bulletin's ``<ddhhmm>``, dated from the arrival stamp."""
    arrived: datetime
    """The gateway's own timestamp in the file name (its ``LastModified``
    is the same to within seconds)."""
    correction: str | None


def parse_file_name(name: str) -> BulletinName | None:
    """The bulletin a gateway object's name describes, or None when the
    name is not the WMO convention (nothing else is fetched, but a file
    dropped into a raw directory by hand should not crash a build)."""
    match = FILE_NAME.match(name)
    if match is None:
        return None
    try:
        arrived = datetime.strptime(match.group("arrived"), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    ddhhmm = match.group("ddhhmm")
    day, hour, minute = int(ddhhmm[:2]), int(ddhhmm[2:4]), int(ddhhmm[4:])
    nominal = _nominal_from_day(day, hour, minute, arrived)
    if nominal is None:
        return None
    correction = match.group("bbb")
    header = f"{match.group('designator')} {match.group('centre')} {ddhhmm}"
    return BulletinName(
        header=header if correction is None else f"{header} {correction}",
        nominal=nominal,
        arrived=arrived,
        correction=correction,
    )


def _nominal_from_day(day: int, hour: int, minute: int, arrived: datetime) -> datetime | None:
    """``<ddhhmm>`` names a day of the month, not a month: the month is
    the one that puts the bulletin nearest its arrival, which is the
    current one except across a month boundary."""
    if not 1 <= day <= 31 or hour > 23 or minute > 59:
        return None
    best: datetime | None = None
    for offset in (0, -1, 1):
        year, month = divmod(arrived.year * 12 + (arrived.month - 1) + offset, 12)
        try:
            candidate = datetime(year, month + 1, day, hour, minute, tzinfo=UTC)
        except ValueError:
            continue
        if best is None or abs(candidate - arrived) < abs(best - arrived):
            best = candidate
    return best


@dataclass(frozen=True)
class Sounding:
    """One station's profile at one nominal time, in the product's units."""

    key: str
    """The identity key: the five-digit WMO number when the subset carries
    one, else the WIGOS id (``docs/sounding.md`` §3)."""
    wigos: str | None
    """The native WIGOS id (``0-20001-0-51463``) when the subset carries a
    usable one."""
    wmo: str | None
    """``block × 1000 + station`` as five digits, when both are present."""
    time: datetime
    """The nominal (synoptic) time the sounding is filed under."""
    launched: datetime | None
    lat: float
    lon: float
    elev: float | None
    sonde: int | None
    """``radiosondeType``, BUFR code table 0 02 011, verbatim."""
    bulletin: str
    gateway: str
    arrived: datetime
    p: tuple[int, ...]
    z: tuple[int, ...]
    t: tuple[int, ...]
    td: tuple[int, ...]
    wd: tuple[int, ...]
    ws: tuple[int, ...]
    sig: tuple[int, ...]

    @property
    def n(self) -> int:
        return len(self.p)

    @property
    def id(self) -> str:
        """The published station id: the native WIGOS id when there is
        one, else the legacy WIGOS form of the WMO number."""
        if self.wigos is not None:
            return self.wigos
        return f"0-20000-0-{self.wmo}"


def _chunks(entries: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The flat stream cut into subsets. ``subsetNumber`` opens each one
    in an uncompressed message; anything before the first marker (a
    compressed message, which carries no markers) is a chunk of its own."""
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("key") == "subsetNumber":
            if current:
                chunks.append(current)
            current = []
            continue
        current.append(entry)
    if current:
        chunks.append(current)
    return chunks


def _subsets(chunk: list[dict[str, Any]]) -> list[list[tuple[str, Any]]]:
    """One ``(key, value)`` list per subset the chunk holds — one for an
    uncompressed subset, ``numberOfSubsets`` for a compressed message,
    whose every per-subset value arrives as an array."""
    count = 1
    for entry in chunk:
        value = entry.get("value")
        if isinstance(value, list):
            count = max(count, len(value))
    subsets: list[list[tuple[str, Any]]] = []
    for position in range(count):
        walked: list[tuple[str, Any]] = []
        for entry in chunk:
            key = entry.get("key")
            if not isinstance(key, str):
                continue
            value = entry.get("value")
            if isinstance(value, list):
                value = value[position] if position < len(value) else None
            walked.append((key, value))
        subsets.append(walked)
    return subsets


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if math.isnan(value):
        return None
    return float(value)


def _fixed(value: Any, scale: float, array: str) -> int:
    """A BUFR value as the product's fixed point, ``MISSING`` when absent
    or outside ``VALUE_BOUNDS[array]``. Half-up on the magnitude, so −0.5
    and 0.5 round away from zero and a second implementation does not have
    to know Python's banker's rule."""
    number = _number(value)
    if number is None:
        return MISSING
    scaled = number * scale
    fixed = int(math.floor(scaled + 0.5)) if scaled >= 0 else -int(math.floor(-scaled + 0.5))
    low, high = VALUE_BOUNDS[array]
    return fixed if low <= fixed <= high else MISSING


@dataclass
class _Level:
    values: dict[str, Any]

    def holds(self, key: str) -> bool:
        return key in self.values


def _levels(walked: list[tuple[str, Any]]) -> list[dict[str, Any]]:
    levels: list[_Level] = []
    for key, value in walked:
        if key not in LEVEL_KEYS:
            continue
        if not levels or levels[-1].holds(key):
            levels.append(_Level({}))
        levels[-1].values[key] = value
    return [level.values for level in levels]


def _station_identity(header: dict[str, Any]) -> tuple[str, str | None, str | None] | None:
    """``(key, wigos, wmo)``, or None when the subset names no station the
    product can publish (``docs/sounding.md`` §3)."""
    wigos: str | None = None
    local = header.get("wigosLocalIdentifierCharacter")
    series = _number(header.get("wigosIdentifierSeries"))
    issuer = _number(header.get("wigosIssuerOfIdentifier"))
    issue = _number(header.get("wigosIssueNumber"))
    if isinstance(local, (str, int)) and None not in (series, issuer, issue):
        text = str(local).strip()
        if WIGOS_LOCAL.match(text):
            wigos = f"{int(series)}-{int(issuer)}-{int(issue)}-{text}"
    wmo: str | None = None
    block = _number(header.get("blockNumber"))
    station = _number(header.get("stationNumber"))
    if block is not None and station is not None and 1 <= block <= 99 and 0 <= station <= 999:
        wmo = f"{int(block):02d}{int(station):03d}"
    if wmo is not None:
        return wmo, wigos, wmo
    if wigos is not None:
        return wigos, wigos, None
    return None


def _elevation(header: dict[str, Any]) -> float | None:
    """The station's height above mean sea level, the first of three the
    subset carries: the ground under the station
    (``heightOfStationGroundAboveMeanSeaLevel``), then ``height`` (the BUFR
    "height of station"), then the barometer's own height. The ground is
    what a profile's heights are measured from, so it comes first; the
    barometer is last because it sits some metres above it."""
    for key in ("heightOfStationGroundAboveMeanSeaLevel", "height", "heightOfBarometerAboveMeanSeaLevel"):
        value = _number(header.get(key))
        if value is not None:
            return value
    return None


def _launch(header: dict[str, Any], nominal: datetime) -> datetime | None:
    """The subset's clock (``timeSignificance`` 18 — radiosonde launch
    time) when it is a plausible launch for this nominal hour: from 90
    minutes before it to an hour after, which covers both the Chinese
    stations releasing at 23:15 for 00Z and the Japanese ones at 00:16.
    Anything else is a clock the product does not trust, and null."""
    parts = {key: _number(header.get(key)) for key in ("year", "month", "day", "hour", "minute")}
    if any(value is None for value in parts.values()):
        return None
    try:
        launched = datetime(
            int(parts["year"]),  # type: ignore[arg-type]
            int(parts["month"]),  # type: ignore[arg-type]
            int(parts["day"]),  # type: ignore[arg-type]
            int(parts["hour"]),  # type: ignore[arg-type]
            int(parts["minute"]),  # type: ignore[arg-type]
            tzinfo=UTC,
        )
    except ValueError:
        return None
    if not -timedelta(minutes=90) <= launched - nominal <= timedelta(minutes=60):
        return None
    return launched


def _fold_repeated_pressures(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pressure is the product's vertical axis and is strictly
    descending, so two levels reported at one pressure — a significant
    temperature level and a significant wind level at the same height, the
    way a few centres file them — become one: the first level keeps its
    values, takes whatever it is missing from the second, and the two
    significance fields are or-ed together."""
    folded: list[dict[str, Any]] = []
    for level in rows:
        if folded and _number(folded[-1].get("pressure")) == _number(level.get("pressure")):
            kept = folded[-1]
            for key, value in level.items():
                if key == "extendedVerticalSoundingSignificance":
                    first, second = _number(kept.get(key)), _number(value)
                    if first is not None and second is not None:
                        kept[key] = int(first) | int(second)
                        continue
                if _number(kept.get(key)) is None and value is not None:
                    kept[key] = value
            continue
        folded.append(dict(level))
    return folded


@dataclass(frozen=True)
class BulletinResult:
    soundings: tuple[Sounding, ...]
    subsets: int
    dropped: int
    """Subsets skipped: no station identity, or no level with a pressure."""


def parse_bulletin(dump: dict[str, Any], *, name: BulletinName, gateway: str) -> BulletinResult:
    """Every station in one ``bufr_dump -j f`` document, in file order."""
    entries = dump.get("messages")
    if not isinstance(entries, list):
        raise TypeError("bufr dump: no messages array")
    soundings: list[Sounding] = []
    subsets = 0
    dropped = 0
    for chunk in _chunks(entries):
        for walked in _subsets(chunk):
            subsets += 1
            sounding = _parse_subset(walked, name=name, gateway=gateway)
            if sounding is None:
                dropped += 1
                continue
            soundings.append(sounding)
    return BulletinResult(tuple(soundings), subsets, dropped)


def _parse_subset(walked: list[tuple[str, Any]], *, name: BulletinName, gateway: str) -> Sounding | None:
    header: dict[str, Any] = {}
    for key, value in walked:
        if key in LEVEL_KEYS:
            break
        header.setdefault(key, value)
    identity = _station_identity(header)
    if identity is None:
        return None
    key, wigos, wmo = identity
    lat = _number(header.get("latitude"))
    lon = _number(header.get("longitude"))
    if lat is None or lon is None or not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
        return None
    nominal = name.nominal
    rows = [level for level in _levels(walked) if _fixed(level.get("pressure"), 1, "p") != MISSING]
    if not rows:
        return None
    # The subset's own order first (it is the ascent), then by pressure
    # descending, which is the product's one order; Python's sort is
    # stable, so two levels at one pressure keep the ascent's order.
    rows.sort(key=lambda level: -float(_number(level.get("pressure")) or 0.0))
    rows = _fold_repeated_pressures(rows)
    sonde = _number(header.get("radiosondeType"))
    return Sounding(
        key=key,
        wigos=wigos,
        wmo=wmo,
        time=nominal,
        launched=_launch(header, nominal),
        lat=round(lat, 5),
        lon=round(lon, 5),
        elev=_elevation(header),
        sonde=None if sonde is None else int(sonde),
        bulletin=name.header,
        gateway=gateway,
        arrived=name.arrived,
        p=tuple(_fixed(level.get("pressure"), 1, "p") for level in rows),
        z=tuple(_fixed(level.get("nonCoordinateGeopotentialHeight"), 1, "z") for level in rows),
        t=tuple(_fixed(level.get("airTemperature"), 100, "t") for level in rows),
        td=tuple(_fixed(level.get("dewpointTemperature"), 100, "td") for level in rows),
        wd=tuple(_fixed(level.get("windDirection"), 1, "wd") for level in rows),
        ws=tuple(_fixed(level.get("windSpeed"), 10, "ws") for level in rows),
        sig=tuple(_fixed(level.get("extendedVerticalSoundingSignificance"), 1, "sig") for level in rows),
    )


def deduplicate(soundings: list[Sounding]) -> list[Sounding]:
    """One sounding per (station, nominal time). The two gateways carry
    overlapping sets, a station is reformatted into more than one bulletin
    and corrections repeat a bulletin outright, so the rule is: the most
    levels wins, and on a tie the latest arrival — which is the correction
    (``docs/sounding.md`` §3)."""
    best: dict[tuple[str, datetime], Sounding] = {}
    for sounding in soundings:
        key = (sounding.key, sounding.time)
        current = best.get(key)
        if current is None or (sounding.n, sounding.arrived) > (current.n, current.arrived):
            best[key] = sounding
    return sorted(best.values(), key=lambda s: (s.key, s.time))
