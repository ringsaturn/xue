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
from .model import SourceFrame
from .rules import EnsembleIdentity, EnsembleRecord, ensemble_rule_accepts
from .variables import AerosolIdentity, VariableSpec, variable_spec

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
# that we know how to time-stamp. 4.8 adds the statistical interval. 4.48
# (aerosol optical properties: GEFS-Aerosols' whole output) inserts the
# aerosol type and two intervals — 24 octets — between the parameter number
# and the generating process, so its time and surface octets sit that much
# further along; 4.44 and 4.46, the aerosol templates without the optical
# fields, are its subsets and are not read by any source yet.
#
# The ensemble templates (docs/format.md §"Ensemble") share the 4.0 layout
# too and append the statistic: 4.2 the derived forecast at octet 35, 4.6
# the percentile at 35, 4.5 the probability's type and limits at 37-47;
# their interval forms 4.12, 4.10 and 4.9 append 4.8's time range after
# it. Each maps to the octets (1-based, as GRIB2 numbers them, so index
# ``octet - 1`` of the section) of the interval's end time, its range
# count, the first range's process, unit and length.
_INSTANTANEOUS_TEMPLATES = {0, 1}
# 4.8 and 4.11 (an ensemble member's statistic, three octets of member
# fields before the interval): the octets of the interval's end time and the
# first range's process, and the section's minimum length.
_STATISTICAL_TEMPLATES: dict[int, tuple[int, int, int]] = {8: (35, 47, 47), 11: (38, 50, 50)}
_AEROSOL_TEMPLATES = {48}
_AEROSOL_SHIFT = 24
_MISSING_UINT32 = 0xFFFFFFFF
_ENSEMBLE_MINIMUM_LENGTH = {2: 36, 6: 35, 5: 47, 12: 60, 10: 59, 9: 71}
_ENSEMBLE_INTERVALS: dict[int, tuple[int, int, int, int, int]] = {
    # template: (end time, ranges, process, unit, length)
    12: (37, 44, 49, 51, 52),
    10: (36, 43, 48, 50, 51),
    9: (48, 55, 60, 62, 63),
}
_HOURS_UNIT = 1


class UnsupportedTemplate(ConversionError):
    """A record of a product definition template the index cannot read."""


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
    aerosol: AerosolIdentity | None = None
    """The aerosol type and intervals of an aerosol product (template 4.48);
    None for every other template, which no aerosol variable matches."""
    template: int = 0
    """The product definition template number."""
    ensemble: EnsembleIdentity | None = None
    """The statistic of an ensemble product (templates 4.2 / 4.12, 4.6 /
    4.10, 4.5 / 4.9): matched by a ``template-ensemble`` rule alone. None
    for every other template."""
    interval_hours: int | None = None
    """The length in hours of an ensemble interval template's one time
    range; None elsewhere, and where the range is not one, or not in
    hours."""


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


def _parse_limit(body: bytes, offset: int) -> tuple[int, int] | None:
    """One ``(scaleFactor, scaledValue)`` limit of an aerosol interval at
    ``offset``, None when the value is the missing pattern."""
    scale = struct.unpack_from(">b", body, offset)[0]
    scaled_value = struct.unpack_from(">I", body, offset + 1)[0]
    return None if scaled_value == _MISSING_UINT32 else (scale, scaled_value)


def _parse_aerosol(body: bytes) -> AerosolIdentity:
    """The aerosol fields of a template 4.48 body: octets 12-35, the type
    then the size and wavelength intervals, each a code table 4.91 type and
    two limits. An interval of the missing type carries no limits whatever
    the octets hold (docs/format.md §"Band, Producer and Aerosol")."""
    aerosol_type = struct.unpack_from(">H", body, 11)[0]
    size_type = body[13]
    wavelength_type = body[24]
    missing = AerosolIdentity.MISSING_TYPE
    return AerosolIdentity(
        aerosol_type,
        size_type=size_type,
        size_first=None if size_type == missing else _parse_limit(body, 14),
        size_second=None if size_type == missing else _parse_limit(body, 19),
        wavelength_type=wavelength_type,
        wavelength_first=None if wavelength_type == missing else _parse_limit(body, 25),
        wavelength_second=None if wavelength_type == missing else _parse_limit(body, 30),
    )


@dataclass(frozen=True)
class _Product:
    template: int
    category: int
    number: int
    level_type: int
    level_value: float | None
    valid_time: datetime
    statistical: int | None
    aerosol: AerosolIdentity | None = None
    ensemble: EnsembleIdentity | None = None
    interval_hours: int | None = None


def _sign_magnitude(octet: int) -> int:
    return -(octet & 0x7F) if octet & 0x80 else octet


def _parse_probability_limit(body: bytes, offset: int) -> tuple[int, int] | None:
    """One ``(scaleFactor, scaledValue)`` limit of a probability at
    ``offset``: the scale factor sign-and-magnitude, as GRIB2 writes signed
    octets (GDAL reads it so), and None when either half is missing."""
    scale_octet = body[offset]
    scaled_value = struct.unpack_from(">I", body, offset + 1)[0]
    if scale_octet == 0xFF or scaled_value == _MISSING_UINT32:
        return None
    return _sign_magnitude(scale_octet), scaled_value


def _parse_ensemble(template: int, body: bytes) -> EnsembleIdentity:
    if template in (2, 12):
        return EnsembleIdentity.derived(body[34])
    if template in (6, 10):
        return EnsembleIdentity.percentile_of(body[34])
    return EnsembleIdentity.probability(body[36], _parse_probability_limit(body, 37), _parse_probability_limit(body, 42))


def _parse_product(body: bytes, reference_time: datetime, path: Path) -> _Product:
    """Parse a section 4 body (header included) into the record's
    identity and valid time."""
    template = struct.unpack_from(">H", body, 7)[0]
    shift = 0
    aerosol: AerosolIdentity | None = None
    ensemble: EnsembleIdentity | None = None
    interval_hours: int | None = None
    statistical: int | None = None
    if template in _INSTANTANEOUS_TEMPLATES:
        minimum_length = 34
    elif template in _STATISTICAL_TEMPLATES:
        minimum_length = _STATISTICAL_TEMPLATES[template][2]
    elif template in _AEROSOL_TEMPLATES:
        shift = _AEROSOL_SHIFT
        minimum_length = 34 + shift
    elif template in _ENSEMBLE_MINIMUM_LENGTH:
        minimum_length = _ENSEMBLE_MINIMUM_LENGTH[template]
    else:
        raise UnsupportedTemplate(f"unsupported GRIB2 product definition template 4.{template} in {path}")
    if len(body) < minimum_length:
        raise ConversionError(f"GRIB2 product definition section is too short in {path}")
    category, number = body[9], body[10]
    if shift:
        aerosol = _parse_aerosol(body)
    unit_code = body[17 + shift]
    if unit_code not in _TIME_UNIT:
        raise ConversionError(f"unsupported GRIB2 time unit {unit_code} in {path}")
    forecast_time = struct.unpack_from(">i", body, 18 + shift)[0]
    level_type = body[22 + shift]
    scale = struct.unpack_from(">b", body, 23 + shift)[0]
    scaled_value = struct.unpack_from(">I", body, 24 + shift)[0]
    # A missing surface value is the scale octet's all-ones or the value's.
    missing_level = body[23 + shift] == 0xFF or scaled_value == _MISSING_UINT32
    level_value = None if missing_level else scaled_value / (10.0**scale)
    if template in _STATISTICAL_TEMPLATES:
        # Statistical products are valid at the end of the overall interval;
        # the forecast time octets hold the interval start.
        end, process, _ = _STATISTICAL_TEMPLATES[template]
        valid_time = _parse_time(body, end - 1, path)
        statistical = body[process - 1]
    elif template in _ENSEMBLE_INTERVALS:
        end, ranges, process, unit, length = _ENSEMBLE_INTERVALS[template]
        valid_time = _parse_time(body, end - 1, path)
        statistical = body[process - 1]
        if body[ranges - 1] == 1 and body[unit - 1] == _HOURS_UNIT:
            interval_hours = struct.unpack_from(">I", body, length - 1)[0]
    else:
        valid_time = reference_time + forecast_time * _TIME_UNIT[unit_code]
    if template in _ENSEMBLE_MINIMUM_LENGTH:
        ensemble = _parse_ensemble(template, body)
    return _Product(
        template, category, number, level_type, level_value, valid_time, statistical, aerosol, ensemble, interval_hours
    )


def _message_info(band: int, discipline: int, reference_time: datetime, product: _Product) -> MessageInfo:
    return MessageInfo(
        band=band,
        discipline=discipline,
        parameter_category=product.category,
        parameter_number=product.number,
        level_type=product.level_type,
        level_value=product.level_value,
        reference_time=reference_time,
        valid_time=product.valid_time,
        statistical_process=product.statistical,
        aerosol=product.aerosol,
        template=product.template,
        ensemble=product.ensemble,
        interval_hours=product.interval_hours,
    )


def product_identity(body: bytes, discipline: int, reference_time: datetime, source: str) -> MessageInfo:
    """The identity of one message from its product definition section
    ``body`` (header included), for a reader that walks message heads of
    its own (:mod:`xuebuild.cmageps`). Raises :class:`UnsupportedTemplate`
    for a template the index does not read."""
    return _message_info(1, discipline, reference_time, _parse_product(body, reference_time, Path(source)))


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
            product: _Product | None = None
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
            messages.append(_message_info(len(messages) + 1, discipline, reference_time, product))
            offset += total_length
            handle.seek(offset)
    if not messages:
        raise ConversionError(f"no GRIB2 messages in {path}")
    return messages


def message_spans(path: Path) -> list[tuple[int, int]]:
    """``(offset, length)`` of every GRIB2 message in a file, in band order
    — the same order :func:`index_messages` reports their identities in, so
    the two zip into a record index of a multi-field file that has no
    sidecar (Météo-France's ARPEGE packages), from which the fetch cuts one
    frame's records."""
    spans: list[tuple[int, int]] = []
    try:
        size = path.stat().st_size
        handle = path.open("rb")
    except OSError as exc:
        raise ConversionError(f"cannot read GRIB input {path}: {exc}") from exc
    with handle:
        offset = 0
        while offset < size:
            indicator = _read_exact(handle, 16, path, "indicator section")
            if indicator[:4] != b"GRIB" or indicator[7] != 2:
                raise ConversionError(f"no GRIB2 message at offset {offset} in {path}")
            total_length = struct.unpack_from(">Q", indicator, 8)[0]
            if total_length < 16 + 4 or offset + total_length > size:
                raise ConversionError(f"GRIB2 message length is out of bounds in {path}")
            spans.append((offset, total_length))
            offset += total_length
            handle.seek(offset)
    return spans


def message_head(head: bytes, source: str) -> tuple[int, MessageInfo]:
    """The total length and the identity of the GRIB2 message whose first
    bytes are ``head`` — enough of it to reach the product definition
    section — so a multi-field object with no sidecar can be indexed by
    Range reads of each message's head instead of a download of the whole
    (the ARPEGE package that carries the terrain). ``source`` names the
    object in errors."""
    label = Path(source)
    if len(head) < 16 or head[:4] != b"GRIB" or head[7] != 2:
        raise ConversionError(f"no GRIB2 message head in {source}")
    total_length = struct.unpack_from(">Q", head, 8)[0]
    reference_time: datetime | None = None
    position = 16
    while position + 5 <= min(len(head), total_length - 4):
        section_length, section_number = struct.unpack_from(">IB", head, position)
        if section_length < 5:
            raise ConversionError(f"GRIB2 section length is out of bounds in {source}")
        body = head[position : position + section_length]
        if section_number in (1, 4) and len(body) < section_length:
            break
        if section_number == 1:
            reference_time = _parse_time(body, 12, label)
        elif section_number == 4:
            if reference_time is None:
                raise ConversionError(f"GRIB2 product definition precedes identification in {source}")
            return total_length, _message_info(1, head[6], reference_time, _parse_product(body, reference_time, label))
        position += section_length
    raise ConversionError(f"the first {len(head)} bytes of a message in {source} hold no product definition")


def matches_variable(spec: VariableSpec, message: MessageInfo) -> bool:
    """Whether ``message`` carries ``spec``'s quantity under its identity,
    an alias or an alternate — the header matcher's own test."""
    return _matches(spec, message)


def _matching_unit(spec: VariableSpec, message: MessageInfo) -> str | None:
    """The GDAL unit of ``message`` when it carries ``spec``'s quantity —
    under the registered identity or one of its alias triples (the same
    surface, the same statistical process), or under one of its whole
    alternate identities — and None when it does not. The unit is the
    identity's own: an alternate may spell it differently (ECMWF's cloud
    cover fraction). An aerosol product matches on its aerosol identity
    too, whole, and only a variable registered with one matches it. An
    ensemble product matches only a ``template-ensemble`` rule of the
    variable's — the same test the GDAL pass applies to the assembled
    template (:mod:`xuebuild.gdal`) — and a variable registered as a
    statistic of an ensemble matches nothing else."""
    if message.ensemble is not None:
        record = EnsembleRecord(
            template=message.template,
            discipline=message.discipline,
            category=message.parameter_category,
            number=message.parameter_number,
            level_type=message.level_type,
            level_value=message.level_value,
            ensemble=message.ensemble,
            interval_hours=message.interval_hours,
            statistical=message.statistical_process,
        )
        accepted = any(
            rule.rule == "template-ensemble" and ensemble_rule_accepts(rule, record) for rule in spec.gdal_rules or ()
        )
        return spec.gdal_unit if accepted else None
    if spec.grib2_ensemble is not None:
        return None
    triple = (message.discipline, message.parameter_category, message.parameter_number)
    if (
        (triple == (spec.grib2_discipline, spec.grib2_category, spec.grib2_number) or triple in spec.grib2_aliases)
        and message.level_type == spec.grib2_level_type
        and (spec.grib2_level_value is None or message.level_value == spec.grib2_level_value)
        and message.statistical_process == spec.grib2_statistical
        and message.aerosol == spec.grib2_aerosol
    ):
        return spec.gdal_unit
    if message.aerosol is not None:
        return None
    for alternate in spec.grib2_alternates:
        if (
            triple == alternate.triple
            and message.level_type == alternate.level_type
            and (alternate.level_value is None or message.level_value == alternate.level_value)
            and message.statistical_process == alternate.statistical
        ):
            return alternate.gdal_unit or spec.gdal_unit
    return None


def _matches(spec: VariableSpec, message: MessageInfo) -> bool:
    return _matching_unit(spec, message) is not None


def inspect_grib_fast(
    path: Path,
    variable_ids: tuple[str, ...],
    *,
    optional_ids: tuple[str, ...] = (),
) -> dict[str, SourceFrame]:
    """Locate every requested variable from the GRIB2 headers alone.

    Mirrors :func:`xue.gdal.inspect_grib_multi`: variables in ``optional_ids``
    may be absent, more than one match is an error. Units are the fixed
    GDAL-normalized strings from the variable table — the identity's own, or
    the alternate's the record matched under (validated against a real
    gdalinfo pass once per run by the converter)."""
    if not path.is_file():
        raise ConversionError(f"GRIB input does not exist: {path}")
    messages = index_messages(path)
    frames: dict[str, SourceFrame] = {}
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
        unit = _matching_unit(spec, message)
        assert unit is not None
        frames[variable_id] = SourceFrame(
            path,
            message.band,
            variable_id,
            message.reference_time,
            message.valid_time,
            int(delta),
            unit,
        )
    return frames
