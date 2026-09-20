from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .errors import DownloadError
from .variables import VariableSpec


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
    alternate_fields: tuple[str, ...] = (),
    qualifier: str = "",
) -> ByteRange:
    """Byte range of the one record ``target_field`` names. When it names
    none, each of ``alternate_fields`` is tried in turn — the same quantity
    under another product's spelling — and the first that names exactly one
    record wins; a phrase that names several is an error whichever it is.
    A ``qualifier`` is a second phrase the line must carry as well, for a
    record the field and surface do not name alone (the aerosol type and
    wavelength a GEFS-Aerosols line spells out after the forecast hour),
    compared without regard to case: the sidecars of one file differ in
    the case of the aerosol type between NOAA's bucket and Google's copy
    (``Dust dry`` and ``Dust Dry``), the offsets being the same."""
    records = parse_index(text)
    qualifier = qualifier.lower()
    for field in (target_field, *alternate_fields):
        matches = [
            index
            for index, record in enumerate(records)
            if field in f":{record.description}"
            and qualifier in f":{record.description}".lower()
            and not any(phrase in record.description for phrase in excluded_phrases)
        ]
        if matches:
            break
    if len(matches) != 1:
        raise DownloadError(
            f"expected exactly one {target_field}{qualifier} record in .idx, found {len(matches)}"
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


# ``<n> hour fcst`` in a wgrib2 description, which is how a whole-run time
# series names the frame a record belongs to. CFSv2 publishes one object per
# variable rather than one per frame, so the forecast hour is what picks a
# record out of the sidecar rather than something the object name already
# said.
_SERIES_HOUR = re.compile(r":(\d+) hour fcst:")


def series_byte_ranges(
    text: str,
    target_field: str,
    *,
    file_size: int | None = None,
    alternate_fields: tuple[str, ...] = (),
) -> dict[int, ByteRange]:
    """Every record of one field in a whole-run time-series ``.idx``, by the
    forecast hour its description names.

    A time-series object holds one variable's entire run — one record per
    frame, and two fields' worth when it carries a vector pair — so the
    field alone names hundreds of records and the hour is what picks one.
    The phrases are the registry's, ``alternate_fields`` tried in turn when
    the first names nothing, exactly as :func:`field_byte_range` does.

    A record's end is the next record's offset; the last record of the
    sidecar has none, so it is included only when ``file_size`` says where
    the object ends and is left out otherwise. That omission is what makes
    an incomplete run answerable: a sidecar written ahead of its data ends
    on a record whose bytes may not be there, and a caller that has not
    measured the object must not assume they are.
    """
    records = parse_index(text)
    for field in (target_field, *alternate_fields):
        matches = [index for index, record in enumerate(records) if field in f":{record.description}"]
        if matches:
            break
    ranges: dict[int, ByteRange] = {}
    for index in matches:
        hour_match = _SERIES_HOUR.search(f":{records[index].description}")
        if hour_match is None:
            raise DownloadError(
                f"time-series .idx record {records[index].number} names no forecast hour: "
                f"{records[index].description!r}"
            )
        hour = int(hour_match.group(1))
        start = records[index].offset
        if index + 1 < len(records):
            end = records[index + 1].offset - 1
        elif file_size is not None and file_size > start:
            end = file_size - 1
        else:
            continue
        if hour in ranges:
            raise DownloadError(f"time-series .idx names forecast hour {hour} twice for {target_field}")
        ranges[hour] = ByteRange(start, end)
    return ranges


def coalesce_ranges(ranges: Sequence[ByteRange] | Iterable[ByteRange]) -> list[ByteRange]:
    """Byte ranges merged where one begins exactly where the last ended.

    A time-series object stores a field's frames in hour order, so a whole
    run's records are one contiguous span and a run cut short at some hour
    is a span plus whatever the file interleaves after it (CFSv2's wind
    object writes a block of U records, then the same block of V records,
    and repeats). Merging turns a thousand records into a handful of range
    requests without ever downloading a byte the caller did not ask for.
    The input must be sorted by offset.
    """
    merged: list[ByteRange] = []
    for byte_range in ranges:
        if merged and byte_range.start == merged[-1].end + 1:
            merged[-1] = ByteRange(merged[-1].start, byte_range.end)
        elif merged and byte_range.start <= merged[-1].end:
            raise DownloadError("byte ranges to coalesce must be sorted and disjoint")
        else:
            merged.append(byte_range)
    return merged


def ecmwf_field_byte_range(
    text: str,
    param: str,
    *,
    levtype: str = "sfc",
    levelist: str | None = None,
    alternate_params: tuple[str, ...] = (),
) -> ByteRange:
    """Byte range of one field in an ECMWF open data ``.index`` file.

    The index is JSON lines; each line carries the GRIB message's ``_offset``
    and ``_length`` directly, so no next-record arithmetic is needed. A
    surface field is ``levtype`` ``sfc`` with no level; a pressure-level
    field is ``pl`` with its ``levelist`` in hectopascals, one line per
    level, so the level must be named to pick one. When ``param`` names no
    record, each of ``alternate_params`` is tried in turn — the same field
    under the spelling another step of the run gives it (the gust's
    ``10fg3``) — and the first that names exactly one record wins; a
    spelling that names several is an error whichever it is.
    """
    records: list[dict[str, object]] = []
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
        record["_line"] = line_number
        records.append(record)
    matches: list[ByteRange] = []
    for candidate in (param, *alternate_params):
        matches = []
        for record in records:
            if record.get("param") != candidate or record.get("levtype", levtype) != levtype:
                continue
            if levelist is not None and record.get("levelist") != levelist:
                continue
            offset, length = record.get("_offset"), record.get("_length")
            if not isinstance(offset, int) or not isinstance(length, int) or offset < 0 or length <= 0:
                raise DownloadError(f"invalid ECMWF .index byte range on line {record['_line']}")
            matches.append(ByteRange(offset, offset + length - 1))
        if matches:
            break
    if len(matches) != 1:
        field = param if levelist is None else f"{param} at {levelist} hPa"
        raise DownloadError(f"expected exactly one {field} record in the ECMWF .index, found {len(matches)}")
    return matches[0]


def ecmwf_level_selector(spec: VariableSpec) -> tuple[str, str | None]:
    """The ``(levtype, levelist)`` an ECMWF ``.index`` line carries for a
    registered variable: ``pl`` with the level in hectopascals on an isobaric
    surface (GRIB2 type 100, whose value is pascals), ``sfc`` otherwise —
    mean sea level included, which the index files under ``sfc``."""
    if spec.grib2_level_type == 100:
        if spec.grib2_level_value is None:
            raise DownloadError(f"{spec.id} is isobaric but names no level")
        return "pl", f"{spec.grib2_level_value / 100:g}"
    return "sfc", None
