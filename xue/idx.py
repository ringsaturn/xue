from __future__ import annotations

import json
from dataclasses import dataclass

from .errors import DownloadError


TARGET_FIELD = ":TMP:2 m above ground:"


@dataclass(frozen=True)
class IndexRecord:
    number: int
    offset: int
    description: str


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def parse_index(text: str) -> list[IndexRecord]:
    records: list[IndexRecord] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(":", 2)
        if len(parts) != 3:
            raise DownloadError(f"invalid .idx line {line_number}: {raw_line!r}")
        try:
            number = int(parts[0])
            offset = int(parts[1])
        except ValueError as exc:
            raise DownloadError(f"invalid .idx numbers on line {line_number}") from exc
        if offset < 0:
            raise DownloadError(f"negative byte offset on .idx line {line_number}")
        if records and offset <= records[-1].offset:
            raise DownloadError(".idx byte offsets are not strictly increasing")
        records.append(IndexRecord(number, offset, parts[2]))
    if not records:
        raise DownloadError("empty .idx response")
    return records


def field_byte_range(
    text: str,
    target_field: str,
    *,
    file_size: int | None = None,
    excluded_phrases: tuple[str, ...] = (),
) -> ByteRange:
    records = parse_index(text)
    matches = [
        index
        for index, record in enumerate(records)
        if target_field in f":{record.description}"
        and not any(phrase in record.description for phrase in excluded_phrases)
    ]
    if len(matches) != 1:
        raise DownloadError(
            f"expected exactly one {target_field} record in .idx, found {len(matches)}"
        )
    index = matches[0]
    start = records[index].offset
    if index + 1 < len(records):
        end = records[index + 1].offset - 1
    elif file_size is not None and file_size > start:
        end = file_size - 1
    else:
        raise DownloadError("target is the final .idx record and remote file size is unknown")
    if end < start:
        raise DownloadError("computed an invalid byte range")
    return ByteRange(start, end)


def target_byte_range(text: str, file_size: int | None = None) -> ByteRange:
    return field_byte_range(text, TARGET_FIELD, file_size=file_size)


def ecmwf_field_byte_range(text: str, param: str, *, levtype: str = "sfc") -> ByteRange:
    """Byte range of one field in an ECMWF open data ``.index`` file.

    The index is JSON lines; each line carries the GRIB message's ``_offset``
    and ``_length`` directly, so no next-record arithmetic is needed.
    """
    matches: list[ByteRange] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DownloadError(f"invalid ECMWF .index line {line_number}") from exc
        if not isinstance(record, dict):
            raise DownloadError(f"ECMWF .index line {line_number} is not an object")
        if record.get("param") != param or record.get("levtype", levtype) != levtype:
            continue
        offset, length = record.get("_offset"), record.get("_length")
        if not isinstance(offset, int) or not isinstance(length, int) or offset < 0 or length <= 0:
            raise DownloadError(f"invalid ECMWF .index byte range on line {line_number}")
        matches.append(ByteRange(offset, offset + length - 1))
    if len(matches) != 1:
        raise DownloadError(f"expected exactly one {param} record in the ECMWF .index, found {len(matches)}")
    return matches[0]
