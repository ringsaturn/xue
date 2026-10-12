"""``fire-build``: one round's fetched FIRMS files and the previous round's
sensor files → one immutable round directory and the live pointer.

The product is a rolling seven days, and FIRMS is fetched only as its
last-24-hour files, so each round is a merge rather than a snapshot: the
previous round's ``<sensor>.jsonl`` files are the history, this round's
detections are laid over them keyed by (acquisition time, lat, lon) with
the new one winning — FIRMS revising a detection is believed — and
anything before the window's start falls off. A sensor whose source failed
keeps its history.

Each sensor gets its own file, one line per UTC hour, so the index can name
each hour's byte span and a reader takes a span of hours in one request.

The pointer is withheld only when no source answered, in which case the
previous round stays live.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import pointproduct
from ..airport.fetch import SourceStatus
from ..errors import FireProductError, XueError
from ..stac import write_point_product_documents
from .fetch import read_fetch_record
from .firms import Detection, read_csv
from .schema import (
    INDEX_FILENAME,
    POINTER_FILENAME,
    PRODUCT,
    SCHEMA_VERSION,
    WINDOW_HOURS,
    build_pointer,
    crc32_hex,
    encode_json,
    iso_z,
    read_index,
    round_directory,
    round_name,
    sensor_filename,
    validate_hour_line,
    validate_index,
    write_bytes_atomic,
)
from .sensors import SENSORS, SOURCES

LOG = logging.getLogger(__name__)


def _published(status: SourceStatus) -> dict[str, Any]:
    """A source's status as the index publishes it: where the bytes came
    from, not where this build happened to put them."""
    return {key: value for key, value in status.to_json().items() if key != "file"}


def _read_previous_sensor(output_root: Path, previous_index: dict[str, Any], sensor_id: str) -> list[Detection]:
    """The previous round's detections of one sensor. A file that is
    missing, or whose CRC32 is not the one its index carries, is not fatal
    — the sensor's window starts again from this round's file."""
    described = next((sensor for sensor in previous_index.get("sensors", []) if sensor.get("id") == sensor_id), None)
    if described is None:
        return []
    issued = datetime.fromisoformat(previous_index["issued"])
    path = output_root / round_directory(issued) / described["file"]["path"]
    try:
        body = path.read_bytes()
    except OSError as exc:
        LOG.warning("fire: %s is not readable (%s); the %s window starts again", path, exc, sensor_id)
        return []
    if crc32_hex(body) != described["file"]["crc32"]:
        LOG.warning("fire: %s is not the file its index names; the %s window starts again", path, sensor_id)
        return []
    detections: list[Detection] = []
    for number, line in enumerate(body.decode("ascii").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            hour = json.loads(line)
            validate_hour_line(hour, f"{path.name}:{number}")
        except (json.JSONDecodeError, FireProductError) as exc:
            LOG.warning("fire: %s line %d is not an hour (%s); skipping it", path, number, exc)
            continue
        for position in range(hour["n"]):
            detections.append(
                Detection(
                    hour["hour"] + hour["t"][position],
                    hour["lat"][position],
                    hour["lon"][position],
                    hour["frp"][position],
                    hour["bt"][position],
                    hour["conf"][position],
                    hour["dn"][position],
                )
            )
    return detections


def _read_source(raw_root: Path, status: SourceStatus, source_id: str) -> tuple[dict[str, list[Detection]], SourceStatus]:
    """This round's detections from one fetched file, and its status with
    what it contributed."""
    source = next(source for source in SOURCES if source.id == source_id)
    if not status.ok or status.file is None:
        return {}, status
    try:
        result = read_csv((raw_root / status.file).read_text(encoding="utf-8"), source)
    except (XueError, OSError, UnicodeDecodeError, ValueError) as exc:
        LOG.warning("fire %s: read failed: %s", source.id, exc)
        return {}, SourceStatus(source.id, False, fetched=status.fetched, url=status.url, error=f"parse: {exc}")
    status.detail = {**status.detail, "reports": result.reports, "rejected": result.rejected}
    return result.detections, status


def _hour_line(sensor_id: str, hour: int, detections: list[Detection]) -> dict[str, Any]:
    return {
        "sensor": sensor_id,
        "hour": hour,
        "n": len(detections),
        "t": [detection.time - hour for detection in detections],
        "lat": [detection.lat for detection in detections],
        "lon": [detection.lon for detection in detections],
        "frp": [detection.frp for detection in detections],
        "bt": [detection.bt for detection in detections],
        "conf": "".join(detection.conf for detection in detections),
        "dn": "".join(detection.dn for detection in detections),
    }


def build_product(
    moment: datetime,
    raw_root: Path,
    output_root: Path,
    *,
    previous_index: dict[str, Any] | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read the round's fetched files and the previous round's sensor
    files, write ``fire.<round>/`` — a file per sensor and the index over
    them — and, unless no source answered, the pointer."""
    now = now or datetime.now(UTC)
    directory = output_root / round_directory(moment)
    if (directory / INDEX_FILENAME).exists() and not force:
        raise FireProductError(f"{directory / INDEX_FILENAME} exists; pass --force to rebuild the round")

    record = read_fetch_record(raw_root, moment)
    record_statuses = {status.id: status for status in record.sources} if record is not None else {}
    start = moment - timedelta(hours=WINDOW_HOURS)
    horizon = int(start.timestamp())

    fresh: dict[str, list[Detection]] = defaultdict(list)
    sources: list[SourceStatus] = []
    for source in SOURCES:
        status = record_statuses.get(source.id) or SourceStatus(source.id, False, error="not fetched")
        detections, status = _read_source(raw_root, status, source.id)
        sources.append(status)
        for sensor_id, found in detections.items():
            fresh[sensor_id].extend(found)
    any_ok = any(status.ok for status in sources)

    published_sensors: list[dict[str, Any]] = []
    rows: list[list[Any]] = []
    total = 0
    for sensor in SENSORS:
        merged: dict[tuple[int, int, int], Detection] = {}
        if previous_index:
            for detection in _read_previous_sensor(output_root, previous_index, sensor.id):
                merged[detection.key] = detection
        for detection in fresh.get(sensor.id, []):
            merged[detection.key] = detection
        kept = sorted(detection for detection in merged.values() if detection.time >= horizon)
        if not kept:
            LOG.info("fire %s: no detection in the window; the sensor is left out", sensor.id)
            continue
        by_hour: dict[int, list[Detection]] = defaultdict(list)
        for detection in kept:
            by_hour[detection.time - detection.time % 3600].append(detection)
        lines: list[bytes] = []
        sensor_rows: list[list[Any]] = []
        offset = 0
        position = len(published_sensors)
        for hour in sorted(by_hour):
            detections = by_hour[hour]
            line_payload = _hour_line(sensor.id, hour, detections)
            validate_hour_line(line_payload, f"{sensor.id}[{hour}]")
            line = encode_json(line_payload)
            sensor_rows.append(
                [
                    position,
                    iso_z(datetime.fromtimestamp(hour, UTC)),
                    len(detections),
                    max(detection.frp for detection in detections) / 10,
                    offset,
                    len(line),
                ]
            )
            lines.append(line)
            offset += len(line) + 1  # the newline
        body = b"\n".join(lines) + b"\n"
        filename = sensor_filename(sensor.id)
        write_bytes_atomic(directory / filename, body)
        published_sensors.append(
            sensor.to_json()
            | {
                "latest": iso_z(datetime.fromtimestamp(kept[-1].time, UTC)),
                "count": len(kept),
                "file": {"path": filename, "byteLength": len(body), "crc32": crc32_hex(body)},
            }
        )
        rows.extend(sensor_rows)
        total += len(kept)

    index_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "issued": iso_z(moment),
        "generated": iso_z(now),
        "window": {"start": iso_z(start), "end": iso_z(moment)},
        "sensors": published_sensors,
        "hours": rows,
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
        LOG.warning("fire: no FIRMS file arrived; the pointer is not written")
    stac_paths = write_point_product_documents(
        output_root, product=PRODUCT, index_path=directory / INDEX_FILENAME, live=pointer_path is not None
    )
    return {
        "round": round_name(moment),
        "directory": str(directory),
        "detections": total,
        "sensors": [
            {
                "id": sensor["id"],
                "latest": sensor["latest"],
                "count": sensor["count"],
                "byteLength": sensor["file"]["byteLength"],
            }
            for sensor in published_sensors
        ],
        "sources": index_payload["sources"],
        "pointer": None if pointer_path is None else str(pointer_path),
        "stac": stac_paths,
    }


def load_previous_index(path: Path | None, output_root: Path) -> dict[str, Any] | None:
    """The previous round's index, whose sensor files are this round's
    history: the one given, else the one the local pointer names, else
    nothing (a first build — the window then starts from the last day)."""
    return pointproduct.load_previous_index(
        path,
        output_root,
        product=PRODUCT,
        pointer_filename=POINTER_FILENAME,
        read_index=read_index,
        error=FireProductError,
        log=LOG,
    )
