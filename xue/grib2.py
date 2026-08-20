"""Minimal GRIB2 header index for band discovery.

Walks sections 0/1/4 of every message in a file — just enough to locate each
variable's band number and its reference/valid time without decoding any
data. This replaces the per-file ``gdalinfo -json`` pre-pass (~1 s per file,
minutes per run) with a few KB of reads per message. gdalinfo stays the
reference implementation: the converter cross-checks the first file of every
run against it and hard-errors on any disagreement, and falls back to full
gdalinfo inspection when a file does not parse here.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from .errors import ConversionError
from .model import GribFrame
from .variables import VariableSpec, variable_spec

# Code table 4.4 (indicator of unit of time range) for the units our
# products use; anything else is rejected and triggers the gdalinfo fallback.
_TIME_UNIT = {
    0: timedelta(minutes=1),
    1: timedelta(hours=1),
    2: timedelta(days=1),
    10: timedelta(hours=3),
    11: timedelta(hours=6),
    12: timedelta(hours=12),
    13: timedelta(seconds=1),
}
# Product definition templates whose octets 10-34 share the 4.0 layout and
# that we know how to time-stamp. 4.8 adds the statistical interval.
_INSTANTANEOUS_TEMPLATES = {0, 1, 2}
_STATISTICAL_TEMPLATES = {8, 11, 12}
_MISSING_UINT32 = 0xFFFFFFFF


@dataclass(frozen=True)
class MessageInfo:
    """Identity of one GRIB2 message, 1-based band order."""

    band: int
    discipline: int
    parameter_category: int
    parameter_number: int
    level_type: int
    level_value: float | None
    reference_time: datetime
    valid_time: datetime
    statistical_process: int | None
    """Code table 4.10 process (0 average, 1 accumulation) for statistical
    templates (4.8 family); None for instantaneous products."""


def _read_exact(handle: BinaryIO, count: int, path: Path, label: str) -> bytes:
    data = handle.read(count)
    if len(data) != count:
        raise ConversionError(f"truncated GRIB2 {label} in {path}")
    return data


def _parse_time(body: bytes, offset: int, path: Path) -> datetime:
    year, month, day, hour, minute, second = struct.unpack_from(">HBBBBB", body, offset)
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except ValueError as exc:
        raise ConversionError(f"invalid GRIB2 timestamp in {path}: {exc}") from exc


def _parse_product(body: bytes, reference_time: datetime, path: Path) -> tuple[int, int, int, float | None, datetime, int | None]:
    """Parse a section 4 body (header included) into
    (category, number, level_type, level_value, valid_time, statistical_process)."""
    template = struct.unpack_from(">H", body, 7)[0]
    if template in _INSTANTANEOUS_TEMPLATES:
        statistical: int | None = None
        minimum_length = 34
    elif template in _STATISTICAL_TEMPLATES:
        minimum_length = 47
    else:
        raise ConversionError(f"unsupported GRIB2 product definition template 4.{template} in {path}")
    if len(body) < minimum_length:
        raise ConversionError(f"GRIB2 product definition section is too short in {path}")
    category, number = body[9], body[10]
    unit_code = body[17]
    if unit_code not in _TIME_UNIT:
        raise ConversionError(f"unsupported GRIB2 time unit {unit_code} in {path}")
    forecast_time = struct.unpack_from(">i", body, 18)[0]
    level_type = body[22]
    scale = struct.unpack_from(">b", body, 23)[0]
    scaled_value = struct.unpack_from(">I", body, 24)[0]
    level_value = None if scaled_value == _MISSING_UINT32 else scaled_value / (10.0**scale)
    if template in _STATISTICAL_TEMPLATES:
        # Statistical products are valid at the end of the overall interval
        # (octets 35-41); the forecast time octets hold the interval start.
        valid_time = _parse_time(body, 34, path)
        statistical = body[46]
    else:
        valid_time = reference_time + forecast_time * _TIME_UNIT[unit_code]
    return category, number, level_type, level_value, valid_time, statistical


def index_messages(path: Path) -> list[MessageInfo]:
    """Band-ordered identities of every message in a GRIB2 file.

    Reads only section headers and the identification/product-definition
    bodies, seeking over grid/data payloads."""
    messages: list[MessageInfo] = []
    try:
        size = path.stat().st_size
        handle = path.open("rb")
    except OSError as exc:
        raise ConversionError(f"cannot read GRIB input {path}: {exc}") from exc
    with handle:
        offset = 0
        while offset < size:
            indicator = _read_exact(handle, 16, path, "indicator section")
            if indicator[:4] != b"GRIB":
                raise ConversionError(f"no GRIB magic at offset {offset} in {path}")
            if indicator[7] != 2:
                raise ConversionError(f"GRIB edition {indicator[7]} is unsupported in {path}")
            discipline = indicator[6]
            total_length = struct.unpack_from(">Q", indicator, 8)[0]
            if total_length < 16 + 4 or offset + total_length > size:
                raise ConversionError(f"GRIB2 message length is out of bounds in {path}")
            reference_time: datetime | None = None
            product: tuple[int, int, int, float | None, datetime, int | None] | None = None
            position = 16
            while product is None:
                if position + 5 > total_length - 4:
                    raise ConversionError(f"GRIB2 message has no product definition section in {path}")
                header = _read_exact(handle, 5, path, "section header")
                section_length, section_number = struct.unpack(">IB", header)
                if section_length < 5 or position + section_length > total_length - 4:
                    raise ConversionError(f"GRIB2 section length is out of bounds in {path}")
                if section_number in (1, 4):
                    body = header + _read_exact(handle, section_length - 5, path, "section body")
                    if section_number == 1:
                        if len(body) < 19:
                            raise ConversionError(f"GRIB2 identification section is too short in {path}")
                        reference_time = _parse_time(body, 12, path)
                    else:
                        if reference_time is None:
                            raise ConversionError(f"GRIB2 product definition precedes identification in {path}")
                        product = _parse_product(body, reference_time, path)
                else:
                    handle.seek(section_length - 5, 1)
                position += section_length
            assert reference_time is not None
            category, number, level_type, level_value, valid_time, statistical = product
            messages.append(
                MessageInfo(
                    band=len(messages) + 1,
                    discipline=discipline,
                    parameter_category=category,
                    parameter_number=number,
                    level_type=level_type,
                    level_value=level_value,
                    reference_time=reference_time,
                    valid_time=valid_time,
                    statistical_process=statistical,
                )
            )
            offset += total_length
            handle.seek(offset)
    if not messages:
        raise ConversionError(f"no GRIB2 messages in {path}")
    return messages


def _matches(spec: VariableSpec, message: MessageInfo) -> bool:
    if (
        message.discipline != spec.grib2_discipline
        or message.parameter_category != spec.grib2_category
        or message.parameter_number != spec.grib2_number
        or message.level_type != spec.grib2_level_type
    ):
        return False
    if spec.grib2_level_value is not None and message.level_value != spec.grib2_level_value:
        return False
    return message.statistical_process == spec.grib2_statistical


def inspect_grib_fast(
    path: Path,
    variable_ids: tuple[str, ...],
    *,
    optional_ids: tuple[str, ...] = (),
) -> dict[str, GribFrame]:
    """Locate every requested variable from the GRIB2 headers alone.

    Mirrors :func:`xue.gdal.inspect_grib_multi`: variables in ``optional_ids``
    may be absent, more than one match is an error. Units are the fixed
    GDAL-normalized strings from the variable table (validated against a real
    gdalinfo pass once per run by the converter)."""
    if not path.is_file():
        raise ConversionError(f"GRIB input does not exist: {path}")
    messages = index_messages(path)
    frames: dict[str, GribFrame] = {}
    for variable_id in variable_ids:
        spec = variable_spec(variable_id)
        matches = [message for message in messages if _matches(spec, message)]
        if not matches and variable_id in optional_ids:
            continue
        if len(matches) != 1:
            raise ConversionError(
                f"expected exactly one {spec.grib_element} band for {variable_id} in {path}, found {len(matches)}"
            )
        message = matches[0]
        delta = (message.valid_time - message.reference_time).total_seconds()
        if delta < 0 or delta % 3600:
            raise ConversionError(f"forecast time is not a non-negative whole hour in {path}")
        frames[variable_id] = GribFrame(
            path,
            message.band,
            variable_id,
            message.reference_time,
            message.valid_time,
            int(delta // 3600),
            spec.gdal_unit,
        )
    return frames
