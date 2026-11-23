"""``sounding-build``: the fetched source directories of one issue hour →
one immutable product directory and the live pointer. The only module
where the sources meet.

Every bulletin in the issue's raw directories is decoded, the station
hours the sources and the corrections deliver more than once are
deduplicated, and each station's line is rewritten with its newest four
nominal times — the new soundings merged onto what the previous issue
published for it, since an hour's fetch carries only what arrived in that
hour.

An issue writes two files. ``soundings.jsonl`` is one line per station,
sorted by id, and ``index.json`` spans it: each station's row carries the
byte offset and length of its line, so the shell reads one station with
one range request and an analyst reads the whole file sequentially. A
station nothing new arrived for is carried forward — its line from the
previous issue's file, re-encoded — which is why the publish workflow
pulls that file with the index before building.

A source that failed to list is recorded in ``sources`` and the others
publish. The pointer is withheld only when no source
contributed *and* there was no previous index to carry forward, since a
product with no stations in it would take an empty map live.
"""

from __future__ import annotations

import functools
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import pointproduct
from ..errors import SoundingProductError, XueError
from ..stac import write_point_product_documents
from . import bufr, derive, meteofrance
from .bufr import BUFR_MAGIC
from .fetch import (
    DEFAULT_SOURCES,
    METEOFRANCE_SOURCE,
    NATIVE_SOURCE,
    FetchResult,
    SourceStatus,
    fetched_sources,
    is_native,
    is_source,
    issue_raw_directory,
    read_fetch_record,
    source_directory,
)
from .schema import (
    INDEX_FILENAME,
    POINTER_FILENAME,
    PRODUCT,
    SCHEMA_VERSION,
    SOUNDINGS_FILENAME,
    build_pointer,
    crc32_hex,
    encode_json,
    iso_z,
    issue_directory,
    pointer_directories,
    read_index,
    validate_index,
    validate_station_line,
    write_bytes_atomic,
)

LOG = logging.getLogger(__name__)

KEEP_TIMES = 4
"""Nominal times a station's line carries: four is two days of the 00Z /
12Z pair, which is the window the panel offers."""

STALE_HOURS = 48
"""A station whose newest sounding is older than this is not carried
forward any further; it has stopped reporting, or its identifier has
changed."""

NAMES_PATH = Path(__file__).with_name("stations.json")
"""Station names by WMO number (``docs/sounding.md`` §3). TEMP bulletins
carry none, so they come from this table, checked in rather than fetched:
a catalogue that is down can then never cost an issue its names."""


@functools.cache
def station_names() -> dict[str, str]:
    table = json.loads(NAMES_PATH.read_text(encoding="utf-8"))
    return {str(wmo): str(name) for wmo, name in table["names"].items()}


def station_name(wmo: str | None) -> str | None:
    """The station's name, a function of its WMO number alone, so a line
    carried forward and a line rewritten this hour say the same."""
    return None if wmo is None else station_names().get(wmo)


def _sounding_json(sounding: bufr.Sounding) -> dict[str, Any]:
    levels: dict[str, Any] = {
        "n": sounding.n,
        "reported": sounding.reported,
        "p": list(sounding.p),
        "z": list(sounding.z),
        "t": list(sounding.t),
        "td": list(sounding.td),
        "wd": list(sounding.wd),
        "ws": list(sounding.ws),
        "sig": list(sounding.sig),
    }
    payload: dict[str, Any] = {
        "time": iso_z(sounding.time),
        "launched": None if sounding.launched is None else iso_z(sounding.launched),
        "sondeType": sounding.sonde,
        "bulletin": sounding.bulletin,
        "gateway": sounding.gateway,
        "arrived": iso_z(sounding.arrived),
        **levels,
    }
    payload["derived"] = derive.derive(levels)
    return payload


SOUNDING_KEYS = frozenset(
    "time launched sondeType bulletin gateway arrived n p z t td wd ws sig".split()
)
"""What :func:`_sounding_of` reads of a published sounding."""


def _sounding_of(station: dict[str, Any], item: dict[str, Any], key: str) -> bufr.Sounding:
    """:func:`_sounding_json` read back, so a sounding the previous issue
    published competes with this hour's, and is completed by its parts, by
    the one rule (``bufr.deduplicate``)."""
    return bufr.Sounding(
        key=key,
        wigos=None,
        wmo=station.get("wmo"),
        time=datetime.fromisoformat(item["time"]),
        launched=None if item["launched"] is None else datetime.fromisoformat(item["launched"]),
        lat=station["lat"],
        lon=station["lon"],
        elev=station["elev"],
        sonde=item["sondeType"],
        bulletin=item["bulletin"],
        gateway=item["gateway"],
        arrived=datetime.fromisoformat(item["arrived"]),
        reported=item["reported"],
        p=tuple(item["p"]),
        z=tuple(item["z"]),
        t=tuple(item["t"]),
        td=tuple(item["td"]),
        wd=tuple(item["wd"]),
        ws=tuple(item["ws"]),
        sig=tuple(item["sig"]),
    )


def newest_ascents(soundings: list[bufr.Sounding]) -> list[bufr.Sounding]:
    """A station's line: one ascent per nominal time, newest first, at most
    ``KEEP_TIMES``. None when all it has is the part of one above 100 hPa."""
    ascents = bufr.deduplicate(soundings)
    return sorted(ascents, key=lambda sounding: sounding.time, reverse=True)[:KEEP_TIMES]


def _is_bufr(path: Path) -> bool:
    """Whether the object is a BUFR message at all, by its magic. The
    gateways republish the GTS stream verbatim, so the sounding branches
    also carry plain-text NIL bulletins — a few dozen bytes saying a
    station had nothing to send. They were 7% of the objects the two
    gateways held when this was written."""
    try:
        with path.open("rb") as handle:
            return BUFR_MAGIC in handle.read(256)
    except OSError:
        return False


def _parse_gateway(
    directory: Path, gateway: str, files: list[str], arrived: dict[str, str] | None = None
) -> tuple[list[bufr.Sounding], dict[str, int], str | None]:
    """Decode every bulletin the gateway's fetch left, counting what went
    wrong rather than raising: one corrupt message loses one bulletin.
    The first failure is returned too, so a gateway whose bulletins *all*
    fail — no ``bufr_dump`` on the machine, a bucket serving HTML — is
    reported as a failed source instead of an empty one."""
    if gateway == METEOFRANCE_SOURCE:
        return _parse_meteofrance(directory, files, arrived or {})
    from ..eccodescli import bufr_dump_json  # eccodes is only needed on this path

    soundings: list[bufr.Sounding] = []
    counts = {"bulletins": 0, "nil": 0, "descents": 0, "unreadable": 0, "subsets": 0, "dropped": 0}
    error: str | None = None
    for name in sorted(files):
        path = directory / name
        parsed_name = _bulletin_name(gateway, name, arrived or {})
        if parsed_name is None or not path.is_file():
            counts["unreadable"] += 1
            error = error or f"{name} is not a bulletin this build can read"
            continue
        if not _is_bufr(path):
            # A NIL bulletin: the station had nothing to report and the
            # GTS carries a line of text saying so. Normal traffic, and
            # not worth a bufr_dump.
            counts["nil"] += 1
            continue
        try:
            ascent = _ascent_messages(path)
            if ascent is None:
                counts["descents"] += 1
                continue
            if ascent != path:
                try:
                    dump = bufr_dump_json(ascent)
                finally:
                    ascent.unlink(missing_ok=True)
            else:
                dump = bufr_dump_json(path)
            result = bufr.parse_bulletin(dump, name=parsed_name, gateway=gateway)
        except (XueError, TypeError, ValueError, OSError) as exc:
            LOG.warning("sounding %s: %s does not decode: %s", gateway, name, exc)
            counts["unreadable"] += 1
            error = error or f"{name}: {exc}"
            continue
        counts["bulletins"] += 1
        counts["subsets"] += result.subsets
        counts["dropped"] += result.dropped
        soundings.extend(result.soundings)
    return soundings, counts, error


def _parse_meteofrance(
    directory: Path, files: list[str], arrived: dict[str, str]
) -> tuple[list[bufr.Sounding], dict[str, int], str | None]:
    """:func:`_parse_gateway` for Météo-France's JSON files: a file is a
    ``bulletin``, each ascent object in it a ``subset``, each descent
    object a ``descent``."""
    soundings: list[bufr.Sounding] = []
    counts = {"bulletins": 0, "nil": 0, "descents": 0, "unreadable": 0, "subsets": 0, "dropped": 0}
    error: str | None = None
    for name in sorted(files):
        path = directory / name
        stamp = arrived.get(name)
        try:
            if stamp is None:
                raise ValueError("no arrival time in fetch.json")
            result = meteofrance.parse_file(
                path.read_bytes(), name=name, arrived=datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            LOG.warning("sounding %s: %s does not decode: %s", METEOFRANCE_SOURCE, name, exc)
            counts["unreadable"] += 1
            error = error or f"{name}: {exc}"
            continue
        counts["bulletins"] += 1
        counts["subsets"] += result.ascents
        counts["descents"] += result.descents
        counts["dropped"] += result.dropped
        soundings.extend(result.soundings)
    return soundings, counts, error


def _ascent_messages(path: Path) -> Path | None:
    """The object to decode once its descent messages are left out
    (``bufr.DESCENT_SEQUENCE``): the object itself when it holds none,
    None when it holds nothing else, and otherwise a scratch copy of its
    other messages beside it, which the caller removes."""
    data = path.read_bytes()
    messages = bufr.split_messages(data)
    descents = [message for message in messages if bufr.is_descent(message)]
    if not descents:
        return path
    kept = [message for message in messages if not bufr.is_descent(message)]
    if not kept:
        return None
    scratch = path.with_name(f".{path.name}.ascent")
    scratch.write_bytes(b"".join(kept))
    return scratch


def _bulletin_name(gateway: str, name: str, arrived: dict[str, str]) -> bufr.BulletinName | None:
    """A gateway's object is named by the WMO convention; a national
    node's is not, and its arrival is the ``LastModified`` its fetch
    recorded."""
    if not is_native(gateway):
        return bufr.parse_file_name(name)
    stamp = arrived.get(name)
    if stamp is None:
        return None
    try:
        return bufr.native_name(name, datetime.fromisoformat(stamp.replace("Z", "+00:00")))
    except ValueError:
        return None


def _station_payload(
    station_id: str,
    wmo: str | None,
    position: dict[str, Any],
    soundings: list[dict[str, Any]],
) -> dict[str, Any]:
    """One line of the soundings file. It carries no copy of ``sources``:
    the index has that, and repeating it once per station would cost more
    than the stations."""
    payload = {
        "id": station_id,
        "wmo": wmo,
        "name": station_name(wmo),
        "lat": position["lat"],
        "lon": position["lon"],
        "elev": position.get("elev"),
        "soundings": soundings,
    }
    validate_station_line(payload, f"soundings[{station_id}]")
    return payload


def _entry(payload: dict[str, Any], offset: int, length: int) -> dict[str, Any]:
    """The index's row for one station: what a marker layer needs without
    opening the soundings file, plus the byte span that opens it."""
    newest = payload["soundings"][0]
    return {
        "id": payload["id"],
        "wmo": payload["wmo"],
        "name": payload["name"],
        "lat": payload["lat"],
        "lon": payload["lon"],
        "elev": payload["elev"],
        "offset": offset,
        "length": length,
        "latest": newest["time"],
        "times": [sounding["time"] for sounding in payload["soundings"]],
        "headline": derive.headline(newest, newest["derived"]),
    }


def previous_directory(previous_index: dict[str, Any] | None, output_root: Path) -> Path | None:
    """Where the previous issue's soundings file is, when the build can
    see it. ``make live-sounding-index`` pulls it with the index, so in
    the publish workflow it can."""
    if not previous_index:
        return None
    try:
        issued = datetime.fromisoformat(previous_index["issued"])
    except (KeyError, TypeError, ValueError):
        return None
    for name in pointer_directories(issued):
        directory = output_root / name
        if directory.is_dir():
            return directory
    return None


def build_product(
    issue: datetime,
    raw_root: Path,
    output_root: Path,
    *,
    sources: tuple[str, ...] = DEFAULT_SOURCES,
    previous_index: dict[str, Any] | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read the issue's fetched directories, write ``sounding/YYYY/MM/DD/sounding.<issue>/``
    under ``output_root`` and, when anything contributed, the pointer."""
    now = now or datetime.now(UTC)
    raw_directory = issue_raw_directory(raw_root, issue)
    directory = output_root / issue_directory(issue)
    if (directory / INDEX_FILENAME).exists() and not force:
        raise SoundingProductError(
            f"{directory / INDEX_FILENAME} exists; pass --force to rebuild the issue"
        )

    statuses: list[SourceStatus] = []
    soundings: list[bufr.Sounding] = []
    watermark: dict[str, Any] = dict((previous_index or {}).get("watermark", {}))
    for gateway in sources:
        if not is_source(gateway):
            raise XueError(
                f"unknown sounding source {gateway!r}; choose from {', '.join(DEFAULT_SOURCES)} or wis2:<centre>"
            )
    for gateway in fetched_sources(raw_directory, sources):
        result: FetchResult | None = read_fetch_record(raw_directory, gateway)
        if result is None:
            statuses.append(SourceStatus(gateway, False, error="not fetched"))
            continue
        status = result.status
        if status.ok:
            found, counts, error = _parse_gateway(
                source_directory(raw_directory, gateway), gateway, result.files, result.arrived
            )
            if counts["bulletins"] == 0 and counts["unreadable"] > 0:
                LOG.warning("sounding %s: no bulletin decoded: %s", gateway, error)
                status = SourceStatus(
                    gateway,
                    False,
                    fetched=status.fetched,
                    url=status.url,
                    # The watermark is deliberately not advanced: the
                    # objects were downloaded and could not be read, so the
                    # next hour lists them again rather than stepping past
                    # them for good.
                    watermark=None,
                    error=f"parse: {error}",
                    detail=status.detail | counts,
                )
            else:
                soundings.extend(found)
                status.detail.update(counts)
                status.detail["soundings"] = len(found)
        if gateway == NATIVE_SOURCE:
            pass  # the selector lists no objects, so it has no watermark
        elif status.watermark is not None:
            watermark[gateway] = iso_z(status.watermark)
        elif gateway not in watermark:
            watermark[gateway] = None
        statuses.append(status)

    fresh: dict[str, list[bufr.Sounding]] = {}
    for sounding in soundings:
        fresh.setdefault(sounding.key, []).append(sounding)

    previous = _read_previous_soundings(output_root, previous_index)
    stale_before = issue - timedelta(hours=STALE_HOURS)

    lines: dict[str, list[dict[str, Any]]] = {}
    for station_id, station in previous.items():
        lines.setdefault(station.get("wmo") or station_id, []).append(station)

    stations: dict[str, dict[str, Any]] = {}
    carried: set[str] = set()
    copied = 0
    # A station is one station by its WMO number, but its sources do not
    # agree on its id: one carries the native WIGOS identifier, another the
    # legacy form. Whatever it was published under is its history, and an
    # issue that published it twice is put right, new ascent or not.
    for key in sorted(fresh.keys() | {key for key, held in lines.items() if len(held) > 1}):
        group = fresh.get(key, [])
        held = list(lines.get(key, []))
        # A line published while the station had no WMO number is under its
        # identifier alone.
        unnumbered = {sounding.id for sounding in group} - {key}
        held += [previous[name] for name in sorted(unnumbered) if name in previous and not previous[name].get("wmo")]
        carried |= {station["id"] for station in held}
        history = [_sounding_of(station, item, key) for station in held for item in station["soundings"]]
        kept = newest_ascents(group + history)
        if not kept or kept[0].time < stale_before:
            continue
        station_id = _published_id(group, held)
        wmo = next((item.wmo for item in group + history if item.wmo is not None), None)
        position = {"lat": kept[0].lat, "lon": kept[0].lon, "elev": kept[0].elev}
        stations[station_id] = _station_payload(
            station_id, wmo, position, [_sounding_json(sounding) for sounding in kept]
        )
        copied += not group

    for station_id, station in previous.items():
        if station_id in carried or not station.get("soundings"):
            continue
        if datetime.fromisoformat(station["soundings"][0]["time"]) < stale_before:
            continue
        station["name"] = station_name(station.get("wmo"))
        validate_station_line(station, f"soundings[{station_id}]")
        stations[station_id] = station
        copied += 1

    # One line per station, sorted by id, and the index's row for it
    # carries the span of the object alone — not the newline — so a
    # reader's range request slices out something that parses as JSON on
    # its own.
    lines: list[bytes] = []
    entries: list[dict[str, Any]] = []
    offset = 0
    for station_id in sorted(stations):
        payload = stations[station_id]
        line = encode_json(payload)
        entries.append(_entry(payload, offset, len(line)))
        lines.append(line)
        offset += len(line) + 1  # the newline
    soundings_bytes = b"\n".join(lines) + (b"\n" if lines else b"")
    write_bytes_atomic(directory / SOUNDINGS_FILENAME, soundings_bytes)

    index = {
        "schemaVersion": SCHEMA_VERSION,
        "issued": iso_z(issue),
        "generated": iso_z(now),
        "watermark": watermark,
        "soundings": {
            "path": SOUNDINGS_FILENAME,
            "byteLength": len(soundings_bytes),
            "crc32": crc32_hex(soundings_bytes),
        },
        "stations": entries,
        "sources": [status.to_json() for status in statuses],
    }
    validate_index(index)
    index_bytes = encode_json(index)
    write_bytes_atomic(directory / INDEX_FILENAME, index_bytes)

    contributed = any(status.ok for status in statuses) or copied > 0
    pointer_path: Path | None = None
    if contributed and entries:
        pointer = build_pointer(issue, f"{issue_directory(issue)}/{INDEX_FILENAME}", index_bytes)
        pointer_path = output_root / POINTER_FILENAME
        write_bytes_atomic(pointer_path, encode_json(pointer))
    else:
        LOG.warning("sounding: no source contributed and nothing carried forward; the pointer is not written")
    # The STAC face of the issue just written (docs/stac.md §"Point
    # products"), a pure function of the index on disk. The Collection and
    # the live Item follow the pointer: an issue nothing took live gets its
    # own Item and nothing else names it.
    stac_paths = write_point_product_documents(
        output_root,
        product=PRODUCT,
        index_path=directory / INDEX_FILENAME,
        live=pointer_path is not None,
    )
    return {
        "issue": issue.strftime("%Y%m%d%H"),
        "directory": str(directory),
        "stations": len(entries),
        "fresh": len(entries) - copied,
        "copied": copied,
        "soundings": sum(len({sounding.time for sounding in group}) for group in fresh.values()),
        "byteLength": len(soundings_bytes),
        "watermark": watermark,
        "sources": index["sources"],
        "pointer": None if pointer_path is None else str(pointer_path),
        "stac": stac_paths,
    }


def _published_id(group: list[bufr.Sounding], held: list[dict[str, Any]]) -> str:
    """The station's published id: the native WIGOS identifier it was
    published under, else one a sounding of the hour carries, else the
    legacy WIGOS form of the WMO number (``docs/sounding.md`` §3)."""
    ids = [station["id"] for station in held] + [item.wigos for item in group if item.wigos is not None]
    wmo = next((item.wmo for item in group if item.wmo is not None), None) or next(
        (station["wmo"] for station in held if station.get("wmo")), None
    )
    legacy = f"0-20000-0-{wmo}"
    return next((station_id for station_id in ids if station_id != legacy), legacy)


def _read_previous_soundings(
    output_root: Path, previous_index: dict[str, Any] | None
) -> dict[str, dict[str, Any]]:
    """The previous issue's stations, by id, read line by line out of the
    ``soundings.jsonl`` its index names.

    A file that cannot be read is not fatal: the issue publishes what
    arrived this hour and every station's window starts again, which is
    the state of a first build anyway. That is why the publish workflow
    pulls the file with the index and checks its CRC32 before trusting
    it.

    A sounding that is the part of an ascent above 100 hPa alone is left
    behind: it is no ascent (``docs/sounding.md`` §3)."""
    stations: dict[str, dict[str, Any]] = {}
    directory = previous_directory(previous_index, output_root)
    if directory is None or not previous_index:
        return stations
    named = (previous_index.get("soundings") or {}).get("path", SOUNDINGS_FILENAME)
    path = directory / named
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        LOG.warning("sounding: %s is not readable (%s); the window starts again", path, exc)
        return stations
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            station = json.loads(line)
            station_id = station["id"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            LOG.warning("sounding: %s line %d is not a station (%s); it is skipped", path, number, exc)
            continue
        soundings = station.get("soundings")
        if not isinstance(station_id, str) or not isinstance(soundings, list) or not soundings:
            continue
        station["soundings"] = [
            item | {"reported": item.get("reported", item["n"])}
            for item in soundings
            if isinstance(item, dict)
            and SOUNDING_KEYS <= item.keys()
            and item["p"][0] > bufr.PART_TOP_PA
        ]
        if station["soundings"]:
            stations[station_id] = station
    return stations


def load_previous_index(path: Path | None, output_root: Path) -> dict[str, Any] | None:
    """The previous hour's index: the one given, else the one the local
    pointer names, else nothing (a first build, or a fresh checkout)."""
    return pointproduct.load_previous_index(
        path,
        output_root,
        product="sounding",
        pointer_filename=POINTER_FILENAME,
        read_index=read_index,
        error=SoundingProductError,
        log=LOG,
    )


def previous_watermarks(previous_index: dict[str, Any] | None) -> dict[str, datetime | None]:
    """The per-gateway watermark the fetch starts from."""
    watermarks: dict[str, datetime | None] = {}
    for gateway, value in (previous_index or {}).get("watermark", {}).items():
        if isinstance(value, str):
            try:
                watermarks[gateway] = datetime.fromisoformat(value)
            except ValueError:
                watermarks[gateway] = None
        else:
            watermarks[gateway] = None
    return watermarks
