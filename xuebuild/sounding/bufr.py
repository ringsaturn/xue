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
from collections.abc import Sequence
from dataclasses import dataclass, replace
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

DESCENT_SEQUENCE = 309056
"""Table D 3 09 056, the radiosonde *descent*: the profile from the burst
down, its clock the burst's, its position and ``height`` the burst point's.
It shares the ascent's keys, so only the message's template tells it
apart, and a product of ascents leaves it out (Météo-France's node
publishes nothing else)."""

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
MAX_WIND_BIT = 1 << 14
SIGNIFICANT_TEMPERATURE_BIT = 1 << 13
SIGNIFICANT_HUMIDITY_BIT = 1 << 12
SIGNIFICANT_WIND_BIT = 1 << 11

SIGNIFICANT_BITS = (
    SURFACE_BIT
    | STANDARD_BIT
    | TROPOPAUSE_BIT
    | MAX_WIND_BIT
    | SIGNIFICANT_TEMPERATURE_BIT
    | SIGNIFICANT_HUMIDITY_BIT
    | SIGNIFICANT_WIND_BIT
)
"""The levels the classical TEMP set is made of, and the ones a skew-T or
a comparison against a model profile is drawn from. A level carrying any
of these bits is always published."""

THINNING_RATIO = 0.03
"""How far apart two *unflagged* levels have to be in pressure to both be
published: the higher one is kept only when the last kept level's pressure
is at least 3 % above it.

Modern sondes report every second, which is 7 000-odd levels of an ascent
where the classical mandatory-and-significant set is 40 to 150 — detail
far beyond what any viewer draws, and two orders of magnitude of bytes. A
properly flagged ascent passes through this rule untouched; an unflagged
run is capped at about 150 levels between 1000 and 10 hPa. The count
before thinning is published as ``reported`` beside ``n``, so nothing is
hidden."""

STANDARD_PRESSURES = (
    100000, 92500, 85000, 70000, 50000, 40000, 30000, 25000, 20000, 15000, 10000, 7000, 5000, 3000, 2000, 1000
)
"""The standard isobaric surfaces of a TEMP, pascals. A source that sends
the full-resolution ascent with no significance flags (Météo-France's
open data) keeps the measured level nearest each, so the thinned profile
still has a level at 850 and 500 hPa for the derived quantities and the
headline to read. The level is kept as measured and is not flagged: it is
not a level the station reported as standard."""

ANCHOR_TOLERANCE_PA = 100
"""How far from a standard surface the nearest level may lie and still be
kept for it — the same hectopascal ``derive`` reads a surface within. A
one- or two-second ascent has a level every hectopascal or less at 850
hPa, so it always has one."""

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

PART_NAME = re.compile(r"^TEMP_(?P<part>AA|BB|CC|DD)_")
"""A national node's object that is one part of a TEMP converted from text
(``TEMP_DD_72649_<uuid>.bufr4``): A the standard levels and their heights
up to 100 hPa, B the significant levels of the same reach, C and D the
same two above 100 hPa. One ascent is four objects."""

PART_TOP_PA = 10000
"""Where a TEMP is cut in two: parts A and B end at 100 hPa, and no level
of C and D has a higher pressure."""

PART_JOINER = " + "
"""Between the bulletins of an ascent put together from several, in its
``bulletin``. A build reads it back off the previous issue to tell such an
ascent, which a part that arrives later still belongs to, from one that
was sent whole."""

SAME_FLIGHT = timedelta(minutes=30)
"""Two parts whose launch clocks are further apart are of two flights: a
station that lost a balloon releases another for the same hour."""

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
    nominal: datetime | None
    """The bulletin's ``<ddhhmm>``, dated from the arrival stamp. None for
    an object of a national WIS2 node, whose name is no heading: each
    subset's nominal time then comes from its own clock
    (:func:`nominal_from_clock`)."""
    arrived: datetime
    """The gateway's own timestamp in the file name (its ``LastModified``
    is the same to within seconds), or a national node's ``LastModified``."""
    correction: str | None
    part: str | None = None
    """``AA`` … ``DD`` for an object that is one part of a TEMP
    (``PART_NAME``), None for one that is a whole ascent."""


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
    nominal = nominal_from_day(day, hour, minute, arrived)
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


def split_messages(data: bytes) -> list[bytes]:
    """The BUFR messages of one object, by each message's own length.
    Bytes between messages (a GTS envelope) are skipped; a message whose
    length runs past the end is taken as what is there."""
    messages: list[bytes] = []
    start = data.find(BUFR_MAGIC)
    while start != -1 and start + 8 <= len(data):
        length = int.from_bytes(data[start + 4 : start + 7], "big")
        if length < 8:
            start = data.find(BUFR_MAGIC, start + 4)
            continue
        messages.append(data[start : start + length])
        start = data.find(BUFR_MAGIC, start + length)
    return messages


def unexpanded_descriptors(message: bytes) -> tuple[int, ...] | None:
    """Section 3's descriptors as ``FXXYYY`` integers (``309052``), or None
    for a message this reader cannot walk (edition below 3, truncated).
    Read off the bytes because ``bufr_dump -j f`` does not carry them."""
    try:
        edition = message[7]
        if edition not in (3, 4):
            return None
        position = 8
        section1 = int.from_bytes(message[position : position + 3], "big")
        optional = message[position + (9 if edition == 4 else 7)] & 0x80
        position += section1
        if optional:
            position += int.from_bytes(message[position : position + 3], "big")
        section3 = int.from_bytes(message[position : position + 3], "big")
        body = message[position + 7 : position + section3]
    except IndexError:
        return None
    if section3 < 7 or len(body) < section3 - 7:
        return None
    descriptors: list[int] = []
    for offset in range(0, len(body) - 1, 2):
        value = int.from_bytes(body[offset : offset + 2], "big")
        descriptors.append((value >> 14) * 100000 + ((value >> 8) & 0x3F) * 1000 + (value & 0xFF))
    return tuple(descriptors)


def is_descent(message: bytes) -> bool:
    descriptors = unexpanded_descriptors(message)
    return descriptors is not None and DESCENT_SEQUENCE in descriptors


def native_name(name: str, arrived: datetime) -> BulletinName:
    """A national WIS2 node's object, which carries no WMO heading
    (``WIGOS_0-20000-0-07110_20261108T011100.bufr4``, ``TEMP_AA_17351_<uuid>.bufr4``,
    ``IUSG01WSSS060000.bufr``…): the object name stands in for the heading,
    and the arrival is the object's ``LastModified``."""
    return BulletinName(
        header=name, nominal=None, arrived=arrived.astimezone(UTC), correction=None, part=part_of(name)
    )


def part_of(name: str) -> str | None:
    """Which part of a TEMP an object's name says it is, or None."""
    match = PART_NAME.match(name)
    return None if match is None else match.group("part")


def content_part(p: Sequence[int], z: Sequence[int], sig: Sequence[int]) -> str | None:
    """Which part of a TEMP a sounding's levels make it, or None when they
    run through 100 hPa. The bulletins do not say: centres send the four
    parts under headings of their own choosing, and a full-resolution
    ascent as two halves. C and D hold nothing below 100 hPa and A and B
    nothing above it, and A and C are the ones that report heights (the
    surface's aside, which is the station's)."""
    upper = p[0] <= PART_TOP_PA
    if not upper and p[-1] < PART_TOP_PA:
        return None
    heights = any(
        height != MISSING and (flags == MISSING or not flags & SURFACE_BIT) for height, flags in zip(z, sig)
    )
    if upper:
        return "CC" if heights else "DD"
    return "AA" if heights else "BB"


def nominal_from_clock(header: dict[str, Any]) -> datetime | None:
    """The synoptic hour a subset with no heading is filed under, from its
    own clock: the six-hourly hour at or before three hours after it. A
    launch from 21:00 to 02:59 is the 00Z ascent, which is the window the
    gateways' headings put on the same stations (Chinese stations release
    at 23:15, French ones as late as 01:15), and a TEMP converted from text
    carries the nominal hour itself."""
    clock = _clock(header)
    return None if clock is None else synoptic_hour(clock)


def synoptic_hour(clock: datetime) -> datetime:
    """The six-hourly hour at or before three hours after ``clock``
    (:func:`nominal_from_clock`)."""
    shifted = clock.astimezone(UTC) + timedelta(hours=3)
    return shifted.replace(hour=shifted.hour - shifted.hour % 6, minute=0, second=0, microsecond=0)


def nominal_from_day(day: int, hour: int, minute: int, arrived: datetime) -> datetime | None:
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
    reported: int
    """Levels the bulletin reported, before thinning (``THINNING_RATIO``).
    ``n`` is how many are published."""
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

    @property
    def part(self) -> str | None:
        """The part of a TEMP its levels are (:func:`content_part`)."""
        return content_part(self.p, self.z, self.sig)

    @property
    def temperatures(self) -> int:
        return sum(value != MISSING for value in self.t)

    @property
    def rank(self) -> tuple[int, int, datetime]:
        """What it is worth against another copy of the same ascent
        (``docs/sounding.md`` §3)."""
        return self.temperatures, self.n, self.arrived


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


def _clock(header: dict[str, Any]) -> datetime | None:
    """The subset's ``year`` … ``minute``, or None when any is missing."""
    parts = {key: _number(header.get(key)) for key in ("year", "month", "day", "hour", "minute")}
    if any(value is None for value in parts.values()):
        return None
    try:
        return datetime(
            int(parts["year"]),  # type: ignore[arg-type]
            int(parts["month"]),  # type: ignore[arg-type]
            int(parts["day"]),  # type: ignore[arg-type]
            int(parts["hour"]),  # type: ignore[arg-type]
            int(parts["minute"]),  # type: ignore[arg-type]
            tzinfo=UTC,
        )
    except ValueError:
        return None


def _launch(header: dict[str, Any], nominal: datetime) -> datetime | None:
    """The subset's clock (``timeSignificance`` 18 — radiosonde launch
    time) when it is a plausible launch for this nominal hour: from 90
    minutes before it to an hour after, which covers both the Chinese
    stations releasing at 23:15 for 00Z and the Japanese ones at 00:16.
    Anything else is a clock the product does not trust, and null."""
    return plausible_launch(_clock(header), nominal)


def plausible_launch(launched: datetime | None, nominal: datetime) -> datetime | None:
    """``launched`` when it lies in the window :func:`_launch` describes,
    else None."""
    if launched is None:
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


def _thin(rows: list[dict[str, Any]], anchors: tuple[int, ...] = ()) -> list[dict[str, Any]]:
    """Drop the levels a reader would never draw (``THINNING_RATIO``).

    The rows arrive in descending pressure. The lowest and the highest are
    always kept, and so is every level flagged as part of the classical
    TEMP set, and the level nearest each of ``anchors`` (pascals) within
    ``ANCHOR_TOLERANCE_PA``. Anything else survives only if it stands at
    least ``THINNING_RATIO`` below the last level kept — kept levels
    included, so a dense patch after a standard level is thinned from that
    level.
    """
    if len(rows) <= 2:
        return rows
    anchored = _nearest_rows(rows, anchors)
    kept = [rows[0]]
    last_pressure = float(_fixed(rows[0].get("pressure"), 1, "p"))
    for position, row in enumerate(rows[1:-1], start=1):
        flags = _fixed(row.get("extendedVerticalSoundingSignificance"), 1, "sig")
        pressure = float(_fixed(row.get("pressure"), 1, "p"))
        flagged = (flags != MISSING and bool(flags & SIGNIFICANT_BITS)) or position in anchored
        if flagged or (pressure > 0 and last_pressure / pressure >= 1.0 + THINNING_RATIO):
            kept.append(row)
            last_pressure = pressure
    kept.append(rows[-1])
    return kept


def _nearest_rows(rows: list[dict[str, Any]], anchors: tuple[int, ...]) -> set[int]:
    """The position of the row nearest each anchor pressure, when one lies
    within ``ANCHOR_TOLERANCE_PA`` of it."""
    found: set[int] = set()
    pressures = [_fixed(row.get("pressure"), 1, "p") for row in rows]
    for anchor in anchors:
        best: int | None = None
        best_gap = ANCHOR_TOLERANCE_PA
        for position, pressure in enumerate(pressures):
            if pressure == MISSING:
                continue
            gap = abs(pressure - anchor)
            if gap <= best_gap:
                best, best_gap = position, gap
        if best is not None:
            found.add(best)
    return found


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
    nominal = name.nominal if name.nominal is not None else nominal_from_clock(header)
    if nominal is None:
        return None
    rows = [level for level in _levels(walked) if _fixed(level.get("pressure"), 1, "p") != MISSING]
    if name.part == "DD":
        rows = _rescaled_part_d(rows)
    sonde = _number(header.get("radiosondeType"))
    return assemble(
        rows,
        key=key,
        wigos=wigos,
        wmo=wmo,
        time=nominal,
        launched=_launch(header, nominal),
        lat=lat,
        lon=lon,
        elev=_elevation(header),
        sonde=None if sonde is None else int(sonde),
        name=name,
        gateway=gateway,
    )


def _rescaled_part_d(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Part D's pressures in pascals. The text form gives them in tenths of
    a hectopascal where part B gives whole ones, and the US node's
    converter reads them as whole: every pressure is ten times what was
    measured, which puts the stratosphere on the ground. A part D that has
    a level at or below 100 hPa is one of those, since a right one has
    none, so the correction ends by itself when the converter is mended."""
    pressures = [_number(level.get("pressure")) or 0.0 for level in rows]
    if not pressures or max(pressures) <= PART_TOP_PA:
        return rows
    return [{**level, "pressure": pressure / 10.0} for level, pressure in zip(rows, pressures)]


def assemble(
    rows: list[dict[str, Any]],
    *,
    key: str,
    wigos: str | None,
    wmo: str | None,
    time: datetime,
    launched: datetime | None,
    lat: float,
    lon: float,
    elev: float | None,
    sonde: int | None,
    name: BulletinName,
    gateway: str,
    anchors: tuple[int, ...] = (),
) -> Sounding | None:
    """One sounding from its levels, each a dict under the BUFR keys
    (``pressure``, ``airTemperature``…): ordered, folded, thinned and put
    in the product's fixed point. Every source's levels pass through here,
    so they are published by one rule. None when no level has a pressure."""
    rows = [level for level in rows if _fixed(level.get("pressure"), 1, "p") != MISSING]
    if not rows:
        return None
    # The subset's own order first (it is the ascent), then by pressure
    # descending, which is the product's one order; Python's sort is
    # stable, so two levels at one pressure keep the ascent's order.
    rows.sort(key=lambda level: -float(_number(level.get("pressure")) or 0.0))
    rows = _fold_repeated_pressures(rows)
    reported = len(rows)
    rows = _thin(rows, anchors)
    return Sounding(
        key=key,
        wigos=wigos,
        wmo=wmo,
        time=time,
        launched=launched,
        lat=round(lat, 5),
        lon=round(lon, 5),
        elev=elev,
        sonde=sonde,
        reported=reported,
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


def _same_flight(one: Sounding, other: Sounding) -> bool:
    """Whether two parts can be of one balloon. A clock that is missing, or
    that is the nominal hour itself (a converter's stand-in), says
    nothing."""
    if one.launched is None or other.launched is None or one.time in (one.launched, other.launched):
        return True
    return abs(one.launched - other.launched) <= SAME_FLIGHT


def _full_resolution(sounding: Sounding) -> bool:
    """Whether a height is reported off the standard surfaces, where a
    TEMP's part A has none but the surface's."""
    return any(
        z != MISSING and p not in STANDARD_PRESSURES and (sig == MISSING or not sig & SURFACE_BIT)
        for p, z, sig in zip(sounding.p, sounding.z, sounding.sig)
    )


def _from_ground(sounding: Sounding) -> Sounding:
    """The sounding without the levels under its surface level that nothing
    was measured on. Part A gives a height for every standard surface down
    to 1000 hPa, those under the ground included: extrapolations, and the
    profile starts at the surface."""
    rows = list(
        zip(sounding.p, sounding.z, sounding.t, sounding.td, sounding.wd, sounding.ws, sounding.sig)
    )
    surface = max((row[0] for row in rows if row[-1] != MISSING and row[-1] & SURFACE_BIT), default=None)
    kept = [
        row for row in rows if surface is None or row[0] <= surface or any(value != MISSING for value in row[2:6])
    ]
    if len(kept) == len(rows):
        return sounding
    p, z, t, td, wd, ws, sig = zip(*kept)
    return replace(
        sounding, reported=sounding.reported - (len(rows) - len(kept)), p=p, z=z, t=t, td=td, wd=wd, ws=ws, sig=sig
    )


def merge_parts(soundings: list[Sounding]) -> Sounding | None:
    """One ascent from the parts of it among a station's soundings for one
    nominal time (``docs/sounding.md`` §3), or None when there is nothing
    to put together: no A and no B, or only one of the four.

    An ascent merged by an earlier build is the part A of this one. Of each
    part the best-ranked copy counts, and B is left out when A is the lower
    half of a full-resolution ascent, B being a selection of its levels.
    The levels are laid on one pressure axis by the rule two levels at one
    pressure already follow: A keeps its values, takes what it is missing
    from B, C and D in that order, and the significance flags are or-ed."""
    parts: dict[str, list[Sounding]] = {}
    for sounding in soundings:
        part = "AA" if PART_JOINER in sounding.bulletin else sounding.part
        if part is not None:
            parts.setdefault(part, []).append(sounding)
    lowest = min(parts, default="CC")
    if lowest not in ("AA", "BB"):
        return None
    base = max(parts[lowest], key=lambda sounding: sounding.rank)
    chosen = [base]
    for part in sorted(parts):
        if part == lowest:
            continue
        flight = [sounding for sounding in parts[part] if _same_flight(base, sounding)]
        best = max(flight, key=lambda sounding: sounding.rank, default=None)
        if best is not None and not (part == "BB" and _full_resolution(base)):
            chosen.append(best)
    levels: dict[int, list[int]] = {}
    contributors = []
    for sounding in chosen:
        added = False
        for p, *values in zip(
            sounding.p, sounding.z, sounding.t, sounding.td, sounding.wd, sounding.ws, sounding.sig
        ):
            kept = levels.get(p)
            if kept is None:
                levels[p] = values
                added = True
                continue
            flags = kept[-1], values[-1]
            levels[p] = [old if old != MISSING else new for old, new in zip(kept, values)]
            if MISSING not in flags:
                levels[p][-1] = flags[0] | flags[1]
            added = added or levels[p] != kept
        if added:
            contributors.append(sounding)
    if len(contributors) < 2:
        return None
    pressures = sorted(levels, reverse=True)
    columns = list(zip(*(levels[p] for p in pressures)))
    thinned = sum(sounding.reported - sounding.n for sounding in contributors)
    merged = replace(
        base,
        reported=len(pressures) + thinned,
        bulletin=PART_JOINER.join(sounding.bulletin for sounding in contributors),
        arrived=max(sounding.arrived for sounding in contributors),
        p=tuple(pressures),
        z=columns[0],
        t=columns[1],
        td=columns[2],
        wd=columns[3],
        ws=columns[4],
        sig=columns[5],
    )
    return _from_ground(merged)


def deduplicate(soundings: list[Sounding]) -> list[Sounding]:
    """One sounding per (station, nominal time). The sources carry
    overlapping sets, a station is reformatted into more than one bulletin
    and corrections repeat a bulletin outright, so the rule is: the most
    temperatures wins, then the most levels, and on a tie the latest
    arrival — which is the correction (``docs/sounding.md`` §3). The parts
    of an ascent also compete as the one they make, and the parts above
    100 hPa never compete alone."""
    hours: dict[tuple[str, datetime], list[Sounding]] = {}
    for sounding in soundings:
        hours.setdefault((sounding.key, sounding.time), []).append(sounding)
    kept: list[Sounding] = []
    for key in sorted(hours):
        candidates = [sounding for sounding in hours[key] if sounding.part not in ("CC", "DD")]
        if not candidates:
            continue
        best = max(candidates, key=lambda sounding: sounding.rank)
        merged = merge_parts(hours[key])
        # A tie goes to the one sent as it is: the parts then hold the same
        # levels.
        if merged is not None and merged.rank[:2] > best.rank[:2]:
            best = merged
        kept.append(_from_ground(best))
    return kept
