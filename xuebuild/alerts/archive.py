"""The alerts archive (``docs/alerts.md`` §12): every alert in force in some
round of a UTC month, once, as the last version that month published, with
when and why it ended — the record, where the five-minute rounds are a
snapshot that lives three hours.

Layout under ``alerts/archive/`` in the archive root::

    catalog.json                the one mutable entry point
    <YYYY>/<MM>/alerts.jsonl    one record per alert id, sorted by id
    <YYYY>/<MM>/index.json      each record's row and byte span

The rounds on the bucket are gone after three hours, so the publisher
feeds the archive as it goes: each round's changes against the round
before are appended to a journal in its state directory, and the journal
is folded into the month files at most once an hour. The fold merges the
month file as a stream, so the memory it takes is the journal's, not the
month's. Nothing in the files carries a build time: the same records give
the same bytes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import urllib.parse
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..errors import DownloadError
from ..fetch import _http_error_code, _request
from ..pointproduct import local_round_directories
from .boxes import DATA_ORIGIN
from .schema import (
    ALERT,
    CATEGORIES,
    CRC32,
    ENDED_REASONS,
    INDEX_FILENAME,
    LEVELS,
    NETWORK,
    PRODUCT,
    ROUND,
    AlertsProductError,
    check_time,
    crc32_hex,
    encode_json,
    iso_z,
    local_round_directory,
    parse_round,
    round_name,
    write_bytes_atomic,
)

LOG = logging.getLogger(__name__)

ARCHIVE_SCHEMA_VERSION = 1
ARCHIVE_DIRECTORY = f"{PRODUCT}/archive"
CATALOG_FILENAME = "catalog.json"
ALERTS_FILENAME = "alerts.jsonl"
JOURNAL_FILENAME = "journal.jsonl"
"""Beside ``alerts/`` in the archive root, never published: the rounds'
changes not yet folded into their months."""

ROW_FIELDS = ("id", "network", "category", "level", "event", "first", "ended", "reason", "offset", "length")
MONTH = re.compile(r"^(\d{4})-(\d{2})$")
CHUNK = 1 << 20


def month_of(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m")


def month_directory(month: str) -> str:
    year, number = month.split("-")
    return f"{ARCHIVE_DIRECTORY}/{year}/{number}"


def month_catalog_path(month: str) -> str:
    """The month's index relative to the catalog."""
    year, number = month.split("-")
    return f"{year}/{number}/{INDEX_FILENAME}"


# --- validation ----------------------------------------------------------


def _time(value: object, label: str) -> None:
    check_time(AlertsProductError, value, label)


def validate_ended(value: object, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise AlertsProductError(f"{label} must be an object or null")
    _time(value.get("time"), f"{label}.time")
    reason = value.get("reason")
    if reason not in ENDED_REASONS:
        raise AlertsProductError(f"{label}.reason must be one of {', '.join(ENDED_REASONS)}")
    by = value.get("by")
    if (by is not None) != (reason == "replaced"):
        raise AlertsProductError(f"{label}.by names the successor of a replaced alert, and only of one")
    if by is not None and (not isinstance(by, str) or not ALERT.match(by)):
        raise AlertsProductError(f"{label}.by is not an alert id")


def validate_record(record: object, label: str) -> None:
    """A line of a month's ``alerts.jsonl``. The alert itself was validated
    when its round was written; here its identity and the fields the
    index repeats are checked."""
    if not isinstance(record, dict):
        raise AlertsProductError(f"{label} must be an object")
    alert = record.get("id")
    match = ALERT.match(alert) if isinstance(alert, str) else None
    if match is None or record.get("network") != match.group(1):
        raise AlertsProductError(f"{label}.id must be an alert id and network its network")
    _time(record.get("first"), f"{label}.first")
    validate_ended(record.get("ended"), f"{label}.ended")
    line = record.get("alert")
    if not isinstance(line, dict) or line.get("id") != alert:
        raise AlertsProductError(f"{label}.alert must be the alert's own line")
    if line.get("category") not in CATEGORIES or line.get("level") not in LEVELS:
        raise AlertsProductError(f"{label}.alert has no category or level")


def _file(value: object, label: str, path: str) -> None:
    if not isinstance(value, dict) or value.get("path") != path:
        raise AlertsProductError(f"{label}.path must be {path!r}")
    if isinstance(value.get("byteLength"), bool) or not isinstance(value.get("byteLength"), int) or value["byteLength"] < 0:
        raise AlertsProductError(f"{label}.byteLength must be a length")
    if not isinstance(value.get("crc32"), str) or not CRC32.match(value["crc32"]):
        raise AlertsProductError(f"{label}.crc32 must be 8 lowercase hex characters")


def validate_month_index(payload: object) -> None:
    if not isinstance(payload, dict) or payload.get("archiveSchemaVersion") != ARCHIVE_SCHEMA_VERSION:
        raise AlertsProductError(f"an archive month index must be archiveSchemaVersion {ARCHIVE_SCHEMA_VERSION}")
    if payload.get("product") != PRODUCT or not isinstance(payload.get("month"), str) or not MONTH.match(payload["month"]):
        raise AlertsProductError("an archive month index names the alerts product and a YYYY-MM month")
    _file(payload.get("alerts"), "index.alerts", ALERTS_FILENAME)
    networks = payload.get("networks")
    if (
        not isinstance(networks, list)
        or networks != sorted(set(networks))
        or not all(isinstance(network, str) and NETWORK.match(network) for network in networks)
    ):
        raise AlertsProductError("index.networks must be sorted unique network ids")
    if "columns" in payload and payload["columns"] != list(ROW_FIELDS):
        raise AlertsProductError(f"index.columns must be {', '.join(ROW_FIELDS)}")
    end = 0
    previous: str | None = None
    for position, row in enumerate(payload.get("rows") if isinstance(payload.get("rows"), list) else [None]):
        label = f"index.rows[{position}]"
        if not isinstance(row, list) or len(row) != len(ROW_FIELDS):
            raise AlertsProductError(f"{label} must be a row of {', '.join(ROW_FIELDS)}")
        alert, network, category, level, _event, first, ended, reason, offset, length = row
        if not isinstance(alert, str) or (previous is not None and alert <= previous):
            raise AlertsProductError("index.rows must be sorted by id and unique")
        previous = alert
        if not isinstance(network, int) or not 0 <= network < len(networks) or not alert.startswith(networks[network] + ":"):
            raise AlertsProductError(f"{label}.network must be the position of the id's network")
        if category not in CATEGORIES or level not in LEVELS:
            raise AlertsProductError(f"{label} has no category or level")
        _time(first, f"{label}.first")
        if (ended is None) != (reason is None):
            raise AlertsProductError(f"{label}: ended and reason are both null or both set")
        if ended is not None:
            _time(ended, f"{label}.ended")
            if reason not in ENDED_REASONS:
                raise AlertsProductError(f"{label}.reason must be one of {', '.join(ENDED_REASONS)}")
        if offset != end or not isinstance(length, int) or length < 1:
            raise AlertsProductError(f"{label}'s span must follow the previous one's")
        end = offset + length + 1
    if end != payload["alerts"]["byteLength"]:
        raise AlertsProductError("index.rows must tile alerts.jsonl")


def validate_catalog(payload: object) -> None:
    if not isinstance(payload, dict) or payload.get("archiveSchemaVersion") != ARCHIVE_SCHEMA_VERSION:
        raise AlertsProductError(f"the archive catalog must be archiveSchemaVersion {ARCHIVE_SCHEMA_VERSION}")
    if payload.get("product") != PRODUCT:
        raise AlertsProductError("the archive catalog names the alerts product")
    months = payload.get("months")
    if not isinstance(months, list):
        raise AlertsProductError("catalog.months must be a list")
    previous: str | None = None
    for position, entry in enumerate(months):
        label = f"catalog.months[{position}]"
        month = entry.get("month") if isinstance(entry, dict) else None
        if not isinstance(month, str) or not MONTH.match(month) or (previous is not None and month <= previous):
            raise AlertsProductError("catalog.months must be sorted unique YYYY-MM months")
        previous = month
        _file(entry, label, month_catalog_path(month))
        if isinstance(entry.get("alerts"), bool) or not isinstance(entry.get("alerts"), int) or entry["alerts"] < 0:
            raise AlertsProductError(f"{label}.alerts must be a count")


def empty_catalog() -> dict[str, Any]:
    return {"archiveSchemaVersion": ARCHIVE_SCHEMA_VERSION, "product": PRODUCT, "months": []}


# --- the journal ------------------------------------------------------------


def _round_lines(directory: Path, index: dict[str, Any]) -> dict[str, dict[str, bytes]]:
    """A round's lines by network and id; a network whose file is missing
    is absent (its lines are unknown, not empty)."""
    lines: dict[str, dict[str, bytes]] = {}
    for network in index["networks"]:
        path = directory / network["file"]["path"]
        try:
            body = path.read_bytes()
        except OSError:
            continue
        lines[network["id"]] = {}
        for row in index["alerts"]:
            if index["networks"][row[1]]["id"] == network["id"]:
                lines[network["id"]][row[0]] = body[row[-2] : row[-2] + row[-1]]
    return lines


def previous_round(output_root: Path, moment: datetime) -> datetime | None:
    """The newest round directory before ``moment`` under ``output_root``
    whose index is there, flat or filed under its day: the live round a
    publish pulled before building."""
    found: datetime | None = None
    for path in local_round_directories(output_root, PRODUCT):
        name = path.name.split(".", 1)[1]
        if not ROUND.match(name) or not (path / INDEX_FILENAME).exists():
            continue
        try:
            candidate = parse_round(name)
        except AlertsProductError:
            continue
        if candidate < moment and (found is None or candidate > found):
            found = candidate
    return found


def journal_round(output_root: Path, moment: datetime) -> list[dict[str, Any]]:
    """The journal entries one round adds: each line that is new or changed
    since the round before (every line, without one in the same month),
    then each ``ended[]`` entry, filed in the month the alert was last in
    force."""
    directory = local_round_directory(output_root, moment)
    index = json.loads((directory / INDEX_FILENAME).read_bytes())
    current = _round_lines(directory, index)
    before = previous_round(output_root, moment)
    previous: dict[str, dict[str, bytes]] = {}
    previous_month = month_of(moment)
    if before is not None:
        previous_month = month_of(before)
        if previous_month == month_of(moment):
            previous_directory = local_round_directory(output_root, before)
            previous = _round_lines(previous_directory, json.loads((previous_directory / INDEX_FILENAME).read_bytes()))
    issued = iso_z(moment)
    month = month_of(moment)
    entries: list[dict[str, Any]] = []
    for network, lines in current.items():
        known = previous.get(network, {})
        for alert in sorted(lines):
            if known.get(alert) == lines[alert]:
                continue
            entries.append({"round": issued, "month": month, "id": alert, "line": json.loads(lines[alert])})
    for row in index["ended"]:
        ended: dict[str, Any] = {"time": issued, "reason": row[2]}
        if len(row) > 3:
            ended["by"] = row[3]
        entries.append({"round": issued, "month": previous_month, "id": row[0], "ended": ended})
    return entries


def read_journal(path: Path) -> list[dict[str, Any]]:
    """The journal's entries; a line a crash cut short is dropped."""
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_bytes().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            LOG.warning("alerts archive: dropping a torn journal line")
            continue
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and isinstance(entry.get("month"), str):
            entries.append(entry)
    return entries


def append_journal(path: Path, entries: list[dict[str, Any]]) -> None:
    if not entries:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        for entry in entries:
            handle.write(encode_json(entry) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


# --- folding ----------------------------------------------------------------


def make_record(alert: str, first: str, ended: dict[str, Any] | None, line: dict[str, Any]) -> dict[str, Any]:
    return {"id": alert, "network": alert.split(":", 1)[0], "first": first, "ended": ended, "alert": line}


def fold(record: dict[str, Any] | None, entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """One alert's record after its journal entries, in order: a line
    replaces the alert and clears the end, the earliest round stays
    ``first``, an end sets ``ended``. An end with no record to land on (the
    alert's lines were never journaled) is dropped."""
    for entry in entries:
        if "line" in entry:
            first = entry["round"] if record is None else min(record["first"], entry["round"])
            record = make_record(entry["id"], first, None, entry["line"])
        elif record is not None:
            record = {**record, "ended": entry["ended"]}
    return record


def _records(path: Path) -> Iterator[tuple[str, bytes]]:
    if not path.exists():
        return
    with path.open("rb") as handle:
        for line in handle:
            line = line.rstrip(b"\n")
            if line:
                yield json.loads(line)["id"], line


def write_month(root: Path, month: str, entries: list[dict[str, Any]]) -> None:
    """Merge a month's journal entries into its ``alerts.jsonl`` as a
    stream, then write its index."""
    by_id: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_id.setdefault(entry["id"], []).append(entry)
    directory = root / month_directory(month)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ALERTS_FILENAME
    rows: list[list[Any]] = []
    networks: set[str] = set()
    offset = 0
    pending = sorted(by_id)
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".alerts-", delete=False) as handle:
        temporary = Path(handle.name)

        def emit(record: dict[str, Any] | None, line: bytes | None = None) -> None:
            nonlocal offset
            if record is None:
                return
            body = line if line is not None else encode_json(record)
            validate_record(record, f"{month} {record['id']}")
            ended = record["ended"]
            rows.append(
                [
                    record["id"],
                    record["network"],
                    record["alert"]["category"],
                    record["alert"]["level"],
                    record["alert"]["native"]["event"],
                    record["first"],
                    ended["time"] if ended else None,
                    ended["reason"] if ended else None,
                    offset,
                    len(body),
                ]
            )
            networks.add(record["network"])
            handle.write(body + b"\n")
            offset += len(body) + 1

        position = 0
        for alert, line in _records(path):
            while position < len(pending) and pending[position] < alert:
                emit(fold(None, by_id[pending[position]]))
                position += 1
            if position < len(pending) and pending[position] == alert:
                emit(fold(json.loads(line), by_id[alert]))
                position += 1
            else:
                emit(json.loads(line), line)
        for alert in pending[position:]:
            emit(fold(None, by_id[alert]))
    os.replace(temporary, path)
    ordered = sorted(networks)
    for row in rows:
        row[1] = ordered.index(row[1])
    crc = _crc32_file(path)
    index = {
        "archiveSchemaVersion": ARCHIVE_SCHEMA_VERSION,
        "product": PRODUCT,
        "month": month,
        "alerts": {"path": ALERTS_FILENAME, "byteLength": offset, "crc32": crc},
        "networks": ordered,
        "columns": list(ROW_FIELDS),
        "rows": rows,
    }
    validate_month_index(index)
    write_bytes_atomic(directory / INDEX_FILENAME, encode_json(index))


def _crc32_file(path: Path) -> str:
    value = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            value = zlib.crc32(chunk, value)
    return f"{value & 0xFFFFFFFF:08x}"


# --- the published side -------------------------------------------------------


@dataclass
class Origin:
    """The published archive, read over HTTPS; None as ``base_url`` reads
    the archive root's own catalog instead (an offline fold)."""

    base_url: str | None
    root: Path

    def _url(self, relative: str, query: str) -> str:
        assert self.base_url is not None
        return urllib.parse.urljoin(self.base_url.rstrip("/") + "/", relative) + f"?{query}"

    def catalog(self) -> dict[str, Any]:
        if self.base_url is None:
            path = self.root / ARCHIVE_DIRECTORY / CATALOG_FILENAME
            return json.loads(path.read_bytes()) if path.exists() else empty_catalog()
        # The one mutable name: a timestamp query keeps an edge from
        # answering with an old one.
        url = self._url(f"{ARCHIVE_DIRECTORY}/{CATALOG_FILENAME}", f"t={datetime.now(UTC).strftime('%Y%m%d%H%M%S')}")
        try:
            body = _request(url, attempts=3, timeout=60, consume=lambda response: response.read())
        except DownloadError as exc:
            if _http_error_code(exc) in (403, 404):
                LOG.info("alerts archive: no published catalog; starting the archive")
                return empty_catalog()
            raise
        catalog = json.loads(body)
        validate_catalog(catalog)
        return catalog

    def pull(self, relative: str, crc: str) -> None:
        """Download one published file into the archive root, streamed, and
        check its CRC32."""
        if self.base_url is None:
            raise AlertsProductError(f"{relative} is published but not on disk, and there is no origin to pull it from")
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.part")

        def consume(response: Any) -> None:
            if getattr(response, "status", None) != 200:
                raise DownloadError(f"expected HTTP 200 for {relative}, received {getattr(response, 'status', None)}")
            with temporary.open("wb") as handle:
                shutil.copyfileobj(response, handle, CHUNK)

        _request(self._url(relative, f"v={crc}"), attempts=3, timeout=300, consume=consume)
        if _crc32_file(temporary) != crc:
            temporary.unlink()
            raise AlertsProductError(f"{relative} arrived with another CRC32 than the {crc} the catalog names")
        os.replace(temporary, destination)


def _ensure_month(root: Path, origin: Origin, published: dict[str, dict[str, Any]], month: str) -> None:
    """A month the published catalog names and the disk lacks is pulled
    first, so the fold continues the published record."""
    directory = root / month_directory(month)
    if month not in published or _complete(directory):
        return
    origin.pull(f"{month_directory(month)}/{INDEX_FILENAME}", published[month]["crc32"])
    index = json.loads((directory / INDEX_FILENAME).read_bytes())
    validate_month_index(index)
    origin.pull(f"{month_directory(month)}/{ALERTS_FILENAME}", index["alerts"]["crc32"])


def _complete(directory: Path) -> bool:
    """A month on disk is its index and the lines that index names; an index
    whose lines a pull did not finish is no record to merge into."""
    try:
        index = json.loads((directory / INDEX_FILENAME).read_bytes())
        return _crc32_file(directory / ALERTS_FILENAME) == index["alerts"]["crc32"]
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _catalog_entry(root: Path, month: str) -> dict[str, Any]:
    body = (root / month_directory(month) / INDEX_FILENAME).read_bytes()
    return {
        "month": month,
        "path": month_catalog_path(month),
        "byteLength": len(body),
        "crc32": crc32_hex(body),
        "alerts": len(json.loads(body)["rows"]),
    }


def local_months(root: Path) -> list[str]:
    found = []
    for path in (root / ARCHIVE_DIRECTORY).glob(f"*/*/{INDEX_FILENAME}"):
        month = f"{path.parent.parent.name}-{path.parent.name}"
        if MONTH.match(month):
            found.append(month)
    return sorted(found)


@dataclass
class FoldResult:
    months: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def fold_journal(root: Path, origin: Origin, moment: datetime) -> FoldResult:
    """Fold the journal into its months, write the catalog, and list what
    differs from the published catalog in upload order. The journal is
    emptied only once every file is written."""
    journal = root / JOURNAL_FILENAME
    entries = read_journal(journal)
    published = {entry["month"]: entry for entry in origin.catalog().get("months", [])}
    by_month: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_month.setdefault(entry["month"], []).append(entry)
    result = FoldResult(months=sorted(by_month))
    for month in result.months:
        _ensure_month(root, origin, published, month)
        write_month(root, month, by_month[month])
    on_disk = local_months(root)
    months = {**published, **{month: _catalog_entry(root, month) for month in on_disk}}
    catalog = {"archiveSchemaVersion": ARCHIVE_SCHEMA_VERSION, "product": PRODUCT, "months": [months[m] for m in sorted(months)]}
    validate_catalog(catalog)
    catalog_bytes = encode_json(catalog)
    catalog_path = root / ARCHIVE_DIRECTORY / CATALOG_FILENAME
    write_bytes_atomic(catalog_path, catalog_bytes)
    for month in on_disk:
        if published.get(month, {}).get("crc32") != months[month]["crc32"]:
            result.written += [f"{month_directory(month)}/{ALERTS_FILENAME}", f"{month_directory(month)}/{INDEX_FILENAME}"]
    if result.written or [published[m] for m in sorted(published)] != catalog["months"]:
        result.written.append(f"{ARCHIVE_DIRECTORY}/{CATALOG_FILENAME}")
    if journal.exists():
        journal.unlink()
    # A month before the previous one that the published catalog already
    # holds as it is on disk is final and safe on the bucket.
    keep_from = month_of(moment.replace(day=1, hour=0, minute=0) - timedelta(days=1))
    for month in on_disk:
        if month < keep_from and month not in result.months and published.get(month, {}).get("crc32") == months[month]["crc32"]:
            shutil.rmtree(root / month_directory(month))
            result.removed.append(month)
    return result


def due(entries: list[dict[str, Any]], moment: datetime) -> bool:
    """A fold is due when the journal holds a round of an earlier hour."""
    hour = moment.astimezone(UTC).strftime("%Y-%m-%dT%H")
    return any(entry["round"][:13] < hour for entry in entries)


def archive_round(
    output_root: Path,
    archive_root: Path,
    moment: datetime,
    *,
    base_url: str | None = DATA_ORIGIN,
    compact: bool = False,
) -> dict[str, Any]:
    """Journal one published round and, when due, fold the journal. The
    report's ``written`` lists the files to upload, relative to
    ``archive_root`` (which mirrors the data root), in upload order."""
    journal = archive_root / JOURNAL_FILENAME
    added = journal_round(output_root, moment)
    append_journal(journal, added)
    report: dict[str, Any] = {"round": round_name(moment), "journaled": len(added), "folded": False, "months": [], "written": [], "removed": []}
    if compact or due(read_journal(journal), moment):
        result = fold_journal(archive_root, Origin(base_url, archive_root), moment)
        report.update(folded=True, months=result.months, written=result.written, removed=result.removed)
    return report
