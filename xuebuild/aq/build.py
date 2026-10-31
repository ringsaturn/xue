"""``aq-build``: one round's fetched files and the previous round's
network files → one immutable round directory and the live pointer.

The product is a rolling 24 hours, and a network hands over only what is
new or revised, so each round is a merge rather than a snapshot: the
previous round's ``<network>.jsonl`` files are the history, this round's
hourly means are laid over them keyed by (station, time) with the new one
winning — AirNow rewrites its recent hours as late data arrive, and the
rewrite is believed — and anything older than
:data:`~.schema.HISTORY_HOURS` before the round falls off the end. A
network that delivered nothing this round keeps its history; a station
with nothing left in the window leaves.

Each network gets two files: its ``.jsonl``, one line per station, and its
``.stations.json``, a row per station with the current values and the byte
span of its line. The index lists the networks and names both, so a map
reads only the tables of the networks it shows.

The pointer is withheld only when no network's observation source
arrived, in which case the previous round stays live.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import pointproduct, stac
from ..airport.fetch import SourceStatus
from ..errors import XueError
from .fetch import read_fetch_record
from .networks import NETWORKS, Network
from .schema import (
    ELEMENTS,
    HISTORY_HOURS,
    INDEX_FILENAME,
    POINTER_FILENAME,
    PRODUCT,
    SCHEMA_VERSION,
    AqProductError,
    build_pointer,
    crc32_hex,
    encode_json,
    iso_z,
    network_filename,
    read_index,
    round_directory,
    round_name,
    stations_filename,
    validate_index,
    validate_station_line,
    validate_stations_file,
    write_bytes_atomic,
)
from .station import NetworkRead

LOG = logging.getLogger(__name__)

META_KEYS = ("name", "names", "lat", "lon", "elev", "rank", "agency")

CURRENT_SECONDS = 3600
"""How far back from a station's newest hour the stations file looks for
each element's current value. A station's pollutants do not all arrive in
the same file — AirNow's newest hour is a few percent short and fills in
over the next two — so a map that read the newest hour alone would blink
an element out for an hour whenever its analyser reported late."""


def _published(status: SourceStatus) -> dict[str, Any]:
    """A source's status as the index publishes it: where the bytes came
    from, not where this build happened to put them."""
    return {key: value for key, value in status.to_json().items() if key != "file"}


def _read_previous_network(
    output_root: Path, previous_index: dict[str, Any], network_id: str
) -> dict[str, dict[str, Any]]:
    """The previous round's stations of one network, by id: their
    metadata and their observations by epoch. A file that cannot be read
    is not fatal — the network's window starts again."""
    entries: dict[str, dict[str, Any]] = {}
    described = next((network for network in previous_index.get("networks", []) if network.get("id") == network_id), None)
    if described is None:
        return entries
    issued = datetime.fromisoformat(previous_index["issued"])
    path = output_root / round_directory(issued) / described["file"]["path"]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        LOG.warning("aq: %s is not readable (%s); the %s window starts again", path, exc, network_id)
        return entries
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            station = json.loads(line)
            station_id = station["id"]
            times = station["time"]
            columns = station.get("obs", {})
            observations = {
                epoch: {key: values[position] for key, values in columns.items()}
                for position, epoch in enumerate(times)
            }
        except (json.JSONDecodeError, KeyError, TypeError, IndexError) as exc:
            LOG.warning("aq: %s line %d is not a station (%s); skipping it", path, number, exc)
            continue
        entries[station_id] = {"meta": {key: station.get(key) for key in META_KEYS}, "obs": observations}
    return entries


def _read_network(network: Network, raw_root: Path, record_statuses: dict[str, SourceStatus]) -> tuple[NetworkRead, list[SourceStatus]]:
    statuses = {
        source: record_statuses.get(source) or SourceStatus(source, False, error="not fetched") for source in network.sources
    }
    try:
        result = network.read(raw_root, statuses)
    except (XueError, OSError, ValueError) as exc:
        LOG.warning("aq %s: read failed: %s", network.id, exc)
        first = network.sources[0]
        statuses[first] = SourceStatus(first, False, fetched=statuses[first].fetched, url=statuses[first].url, error=f"parse: {exc}")
        result = NetworkRead({}, [], False)
    return result, [statuses[source] for source in network.sources]


def _current_values(observations: dict[int, dict[str, Any]], newest: int) -> list[Any]:
    """Each element's newest non-null value at most :data:`CURRENT_SECONDS`
    before ``newest``, in :data:`ELEMENTS` order."""
    recent = sorted((epoch for epoch in observations if epoch >= newest - CURRENT_SECONDS), reverse=True)
    return [
        next((observations[epoch][key] for epoch in recent if observations[epoch].get(key) is not None), None)
        for key in ELEMENTS
    ]


def _station_line(station_id: str, meta: dict[str, Any], observations: dict[int, dict[str, Any]]) -> dict[str, Any]:
    times = sorted(observations)
    columns: dict[str, list[Any]] = {}
    for key in ELEMENTS:
        values = [observations[epoch].get(key) for epoch in times]
        if any(value is not None for value in values):
            columns[key] = values
    return {"id": station_id, **{key: meta.get(key) for key in META_KEYS}, "time": times, "obs": columns}


def build_product(
    moment: datetime,
    raw_root: Path,
    output_root: Path,
    *,
    previous_index: dict[str, Any] | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read the round's fetched files and the previous round's network
    files, write ``aq.<round>/`` — two files per network and the index over
    them — and, unless no network's observations arrived, the pointer."""
    now = now or datetime.now(UTC)
    directory = output_root / round_directory(moment)
    if (directory / INDEX_FILENAME).exists() and not force:
        raise AqProductError(f"{directory / INDEX_FILENAME} exists; pass --force to rebuild the round")

    record = read_fetch_record(raw_root, moment)
    record_statuses = {status.id: status for status in record.sources} if record is not None else {}
    horizon = int((moment - timedelta(hours=HISTORY_HOURS)).timestamp())
    end = int(moment.timestamp())
    issued = iso_z(moment)

    published_networks: list[dict[str, Any]] = []
    sources: list[SourceStatus] = []
    any_ok = False
    station_count = observation_count = 0
    for network in NETWORKS:
        result, statuses = _read_network(network, raw_root, record_statuses)
        sources.extend(statuses)
        any_ok = any_ok or result.ok
        entries = _read_previous_network(output_root, previous_index, network.id) if previous_index else {}
        for station_id, meta in result.stations.items():
            # This round's files win over what the previous round carried:
            # a moved or renamed station is placed by today's metadata.
            entries.setdefault(station_id, {"meta": {}, "obs": {}})["meta"] = meta.to_json()
        for station_id, epoch, elements in result.observations:
            entry = entries.get(station_id)
            if entry is None:
                continue  # the adapter places every station it reports; a stray is dropped
            entry["obs"][epoch] = elements

        lines: list[bytes] = []
        rows: list[list[Any]] = []
        offset = 0
        latest = 0
        for station_id in sorted(entries):
            entry = entries[station_id]
            kept = {
                epoch: elements
                for epoch, elements in entry["obs"].items()
                if horizon <= epoch <= end and any(value is not None for value in elements.values())
            }
            if not kept or entry["meta"].get("lat") is None:
                continue  # nothing observed in the window: the station leaves
            station = _station_line(station_id, entry["meta"], kept)
            validate_station_line(station, f"{network.id}[{station_id}]")
            line = encode_json(station)
            newest_epoch = station["time"][-1]
            latest = max(latest, newest_epoch)
            rows.append(
                [
                    station_id,
                    station["lat"],
                    station["lon"],
                    station["elev"],
                    station["rank"],
                    station["name"],
                    station["agency"],
                    iso_z(datetime.fromtimestamp(newest_epoch, UTC)),
                    *_current_values(kept, newest_epoch),
                    offset,
                    len(line),
                ]
            )
            lines.append(line)
            offset += len(line) + 1  # the newline
            observation_count += len(station["time"])
        if not lines:
            LOG.warning("aq %s: no station observed in the window; the network is left out", network.id)
            continue
        body = b"\n".join(lines) + b"\n"
        table = {
            "schemaVersion": SCHEMA_VERSION,
            "network": network.id,
            "issued": issued,
            "elements": list(ELEMENTS),
            "stations": rows,
        }
        table_bytes = encode_json(table)
        entry = network.to_json() | {
            "latest": iso_z(datetime.fromtimestamp(latest, UTC)),
            "file": {"path": network_filename(network.id), "byteLength": len(body), "crc32": crc32_hex(body)},
            "stations": {
                "path": stations_filename(network.id),
                "byteLength": len(table_bytes),
                "crc32": crc32_hex(table_bytes),
                "count": len(rows),
            },
        }
        validate_stations_file(table, entry, list(ELEMENTS), issued)
        write_bytes_atomic(directory / network_filename(network.id), body)
        write_bytes_atomic(directory / stations_filename(network.id), table_bytes)
        published_networks.append(entry)
        station_count += len(rows)

    index_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "issued": issued,
        "generated": iso_z(now),
        "networks": published_networks,
        "elements": list(ELEMENTS),
        "sources": [_published(status) for status in sources],
    }
    validate_index(index_payload)
    index_bytes = encode_json(index_payload)
    write_bytes_atomic(directory / INDEX_FILENAME, index_bytes)

    pointer_path: Path | None = None
    if any_ok:
        pointer = build_pointer(moment, f"{round_directory(moment)}/{INDEX_FILENAME}", index_bytes)
        pointer_path = output_root / POINTER_FILENAME
        write_bytes_atomic(pointer_path, encode_json(pointer))
    else:
        LOG.warning("aq: no network's observations arrived; the pointer is not written")
    stac_paths: dict[str, str] = {}
    if PRODUCT in stac.POINT_PRODUCTS:
        stac_paths = stac.write_point_product_documents(
            output_root, product=PRODUCT, index_path=directory / INDEX_FILENAME, live=pointer_path is not None
        )
    return {
        "round": round_name(moment),
        "directory": str(directory),
        "stations": station_count,
        "observations": observation_count,
        "networks": [
            {
                "id": network["id"],
                "latest": network["latest"],
                "stations": network["stations"]["count"],
                "byteLength": network["file"]["byteLength"],
            }
            for network in published_networks
        ],
        "sources": index_payload["sources"],
        "pointer": None if pointer_path is None else str(pointer_path),
        "stac": stac_paths,
    }


def load_previous_index(path: Path | None, output_root: Path) -> dict[str, Any] | None:
    """The previous round's index, whose network files are this round's
    history: the one given, else the one the local pointer names, else
    nothing (a first build — the window then starts from what the networks
    still hold)."""
    return pointproduct.load_previous_index(
        path,
        output_root,
        product=PRODUCT,
        pointer_filename=POINTER_FILENAME,
        read_index=read_index,
        error=AqProductError,
        log=LOG,
    )
