"""``alerts-build``: one round's fetched files and the previous round's
index → one immutable round directory and the live pointer.

A round is a snapshot, not a merge: each network's file is what the
network says is in force now, less what has expired by the round's
minute. The previous round matters for two things only. Its index is what
``ended[]`` is measured against — an alert there and not here ended, as
``expired`` (its expiry passed), ``cancelled`` (the network withdrew it
in so many words) or ``gone`` (it is simply no longer listed: replaced by
an update, or lifted without saying). And its network files are what a
network that failed this round, or said it has nothing new, carries over:
the previous lines byte for byte, less the expired, so one bad request
does not blank a country's warnings for five minutes.

The pointer is withheld only when no network's state arrived, in which
case the previous round stays live.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import pointproduct
from .. import stac
from ..airport.fetch import SourceStatus
from ..errors import XueError
from .alert import NetworkRead
from .fetch import read_fetch_record
from .networks import NETWORKS, Network
from .schema import (
    CATEGORIES,
    INDEX_FILENAME,
    POINTER_FILENAME,
    PRODUCT,
    SCHEMA_VERSION,
    AlertsProductError,
    build_pointer,
    crc32_hex,
    encode_json,
    iso_z,
    network_filename,
    read_index,
    round_directory,
    round_name,
    validate_alert_line,
    validate_index,
    write_bytes_atomic,
)

LOG = logging.getLogger(__name__)


def _published(status: SourceStatus) -> dict[str, Any]:
    """A source's status as the index publishes it: where the bytes came
    from, not where this build happened to put them."""
    return {key: value for key, value in status.to_json().items() if key != "file"}


def _read_network(
    network: Network, raw_root: Path, moment: datetime, record_statuses: dict[str, SourceStatus]
) -> tuple[NetworkRead, list[SourceStatus]]:
    statuses = {source: record_statuses.get(source) or SourceStatus(source, False, error="not fetched") for source in network.sources}
    try:
        result = network.read(raw_root, moment, statuses)
    except (XueError, OSError, ValueError, KeyError, TypeError) as exc:
        LOG.warning("alerts %s: read failed: %s", network.id, exc)
        first = network.sources[0]
        statuses[first] = SourceStatus(first, False, fetched=statuses[first].fetched, url=statuses[first].url, error=f"parse: {exc}")
        result = NetworkRead([], False)
    return result, [statuses[source] for source in network.sources]


def _previous_lines(output_root: Path, previous_index: dict[str, Any], network_id: str) -> dict[str, bytes] | None:
    """The previous round's lines of one network by id, as published; None
    when the previous round did not list the network or its file cannot be
    read (the network then has nothing to carry over)."""
    described = next((network for network in previous_index.get("networks", []) if network.get("id") == network_id), None)
    if described is None:
        return None
    path = output_root / round_directory(datetime.fromisoformat(previous_index["issued"])) / described["file"]["path"]
    try:
        body = path.read_bytes()
    except OSError as exc:
        LOG.warning("alerts: %s is not readable (%s); %s has nothing to carry over", path, exc, network_id)
        return None
    lines: dict[str, bytes] = {}
    for number, line in enumerate(body.split(b"\n"), start=1):
        if not line.strip():
            continue
        try:
            lines[json.loads(line)["id"]] = line
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            LOG.warning("alerts: %s line %d is not an alert (%s); skipping it", path, number, exc)
    return lines


def _expired(payload: dict[str, Any], moment: datetime) -> bool:
    expires = payload.get("expires")
    return isinstance(expires, str) and datetime.fromisoformat(expires) <= moment


def _row(payload: dict[str, Any], network: int, box: tuple[float, float, float, float] | None, offset: int, length: int) -> list[Any]:
    west, south, east, north = box if box is not None else (None, None, None, None)
    return [
        payload["id"],
        network,
        payload["category"],
        payload["level"],
        payload["native"]["event"],
        payload["sent"],
        payload["onset"],
        payload["expires"],
        west,
        south,
        east,
        north,
        offset,
        length,
    ]


def _ended(
    previous_index: dict[str, Any] | None,
    current: set[str],
    positions: dict[str, int],
    expired: set[str],
    cancelled: set[str],
    moment: datetime,
) -> list[list[Any]]:
    """The previous round's alerts not in this one, with why."""
    if not previous_index:
        return []
    previous_networks = previous_index.get("networks", [])
    ended: list[list[Any]] = []
    for row in previous_index.get("alerts", []):
        alert, network = row[0], row[1]
        if alert in current:
            continue
        network_id = previous_networks[network]["id"] if 0 <= network < len(previous_networks) else None
        if network_id not in positions:
            continue
        if alert in expired or (isinstance(row[7], str) and datetime.fromisoformat(row[7]) <= moment):
            reason = "expired"
        elif alert in cancelled:
            reason = "cancelled"
        else:
            reason = "gone"
        ended.append([alert, positions[network_id], reason])
    ended.sort(key=lambda entry: entry[0])
    return ended


def build_product(
    moment: datetime,
    raw_root: Path,
    output_root: Path,
    *,
    previous_index: dict[str, Any] | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read the round's fetched files, write ``alerts.<round>/`` — a file
    per network and the index over them — and, unless no network's state
    arrived, the pointer."""
    now = now or datetime.now(UTC)
    directory = output_root / round_directory(moment)
    if (directory / INDEX_FILENAME).exists() and not force:
        raise AlertsProductError(f"{directory / INDEX_FILENAME} exists; pass --force to rebuild the round")

    record = read_fetch_record(raw_root, moment)
    record_statuses = {status.id: status for status in record.sources} if record is not None else {}

    published: list[dict[str, Any]] = []
    rows: list[list[Any]] = []
    sources: list[SourceStatus] = []
    expired: set[str] = set()
    cancelled: set[str] = set()
    carried: list[str] = []
    any_ok = False
    for network in NETWORKS:
        result, statuses = _read_network(network, raw_root, moment, record_statuses)
        sources.extend(statuses)
        any_ok = any_ok or result.ok
        cancelled |= result.cancelled
        lines: dict[str, bytes] = {}
        latest = result.latest
        if result.ok and not result.unchanged:
            for payload in result.alerts:
                lines[payload["id"]] = encode_json(payload)
        else:
            previous = _previous_lines(output_root, previous_index, network.id) if previous_index else None
            if previous is None:
                # Nothing arrived, or "unchanged" against a file this build
                # cannot read: the network is left out, which also makes
                # the next round fetch it in full.
                LOG.warning("alerts %s: no new state and nothing to carry over; the network is left out", network.id)
                continue
            else:
                lines = previous
                carried.append(network.id)
                described = next(entry for entry in previous_index["networks"] if entry["id"] == network.id)  # type: ignore[index]
                if not result.unchanged or latest is None:
                    latest = datetime.fromisoformat(described["latest"]) if described.get("latest") else None

        position = len(published)
        body_lines: list[bytes] = []
        offset = 0
        for alert in sorted(lines):
            line = lines[alert]
            payload = json.loads(line)
            if _expired(payload, moment):
                expired.add(alert)
                continue
            box = validate_alert_line(payload, f"{network.id}[{alert}]")
            rows.append(_row(payload, position, box, offset, len(line)))
            body_lines.append(line)
            offset += len(line) + 1  # the newline
        body = b"".join(line + b"\n" for line in body_lines)
        filename = network_filename(network.id)
        write_bytes_atomic(directory / filename, body)
        published.append(
            network.to_json()
            | {
                "latest": iso_z(latest) if latest is not None else None,
                "file": {"path": filename, "byteLength": len(body), "crc32": crc32_hex(body)},
                "areas": network.areas.to_json() if network.areas is not None else None,
            }
        )

    rows.sort(key=lambda row: row[0])
    positions = {network["id"]: position for position, network in enumerate(published)}
    index_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "issued": iso_z(moment),
        "generated": iso_z(now),
        "networks": published,
        "categories": list(CATEGORIES),
        "alerts": rows,
        "ended": _ended(previous_index, {row[0] for row in rows}, positions, expired, cancelled, moment),
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
        LOG.warning("alerts: no network's state arrived; the pointer is not written")
    stac_paths: dict[str, str] = {}
    # The catalogue describes only the point products it has prose for.
    if PRODUCT in stac.POINT_PRODUCTS:
        stac_paths = stac.write_point_product_documents(
            output_root, product=PRODUCT, index_path=directory / INDEX_FILENAME, live=pointer_path is not None
        )
    return {
        "round": round_name(moment),
        "directory": str(directory),
        "alerts": len(rows),
        "ended": len(index_payload["ended"]),
        "carried": carried,
        "networks": [
            {"id": network["id"], "latest": network["latest"], "byteLength": network["file"]["byteLength"]} for network in published
        ],
        "sources": index_payload["sources"],
        "pointer": None if pointer_path is None else str(pointer_path),
        "stac": stac_paths,
    }


def load_previous_index(path: Path | None, output_root: Path) -> dict[str, Any] | None:
    """The previous round's index: the one given, else the one the local
    pointer names, else nothing (a first build: no ``ended[]``, nothing to
    carry over)."""
    return pointproduct.load_previous_index(
        path,
        output_root,
        product=PRODUCT,
        pointer_filename=POINTER_FILENAME,
        read_index=read_index,
        error=AlertsProductError,
        log=LOG,
    )
