"""Reading one NEXRAD Level 3 digital radial product (N0B, N0G) into the
sweep the polar store holds: the codes on a fixed 0.5° beam grid, and the
header facts the store and the manifest carry.

The layout is the WSR-88D RPG-to-class-1-user ICD (2620001): a WMO
abbreviated heading, the 18-byte message header, the 102-byte product
description block, and the product symbology block — bzip2-compressed when
the description block says so — whose one layer is a packet 16 (digital
radial data array). Only that packet is read; a product carrying anything
else is refused. Codes are copied, never requantized: the description
block's threshold words must describe the codebook ``docs/nexrad.md`` §2
fixes, or the file is refused.
"""

from __future__ import annotations

import bz2
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np

from ..errors import NexradProductError

BEAMS = 720
BEAM_WIDTH = 0.5
"""The fixed beam grid every sweep is binned onto, degrees from north."""

PRODUCTS = {
    # product code: (id, gates, minimum ×10, increment ×10, levels)
    153: ("n0b", 1840, -320, 5, 254),
    154: ("n0g", 1200, -635, 5, 254),
}
"""The products the polar store carries and the threshold words each must
state: a minimum of −32.0 dBZ / −63.5 m/s in tenths, a 0.5 increment and
254 levels over codes 2–255."""

_EPOCH = datetime(1969, 12, 31, tzinfo=UTC)
"""Day 1 of the ICD's Julian date is 1 January 1970."""

_HEADER_BYTES = 18
_DESCRIPTION_BYTES = 102
_PACKET_DIGITAL_RADIAL = 16


@dataclass(frozen=True)
class Sweep:
    product: str
    """``n0b`` or ``n0g``."""
    site_latitude: float
    site_longitude: float
    site_height_m: float
    scan_time: datetime
    """The sweep's own start (a supplemental SAILS sweep's, not its
    volume's)."""
    elevation: float
    """Degrees."""
    vcp: int
    volume_number: int
    elevation_number: int
    codes: np.ndarray
    """``uint8 [720, gates]``: beam ``j`` covers ``[0.5 j, 0.5 (j + 1))``
    degrees; a beam the file does not carry is code 0."""


def _skip_wmo_heading(data: bytes) -> int:
    """Offset of the message header: past the two CR CR LF lines of the WMO
    abbreviated heading when there is one, else 0."""
    if not data[:4].isalpha():
        return 0
    first = data.find(b"\r\r\n")
    second = data.find(b"\r\r\n", first + 3) if first >= 0 else -1
    if first < 0 or second < 0 or second > 64:
        raise NexradProductError("Level 3 file has a malformed WMO heading")
    return second + 3


def _halfword(block: bytes, number: int) -> int:
    """ICD halfword ``number`` (10 … 60) of the product description block,
    signed."""
    offset = (number - 10) * 2
    return struct.unpack_from(">h", block, offset)[0]


def _word(block: bytes, number: int, signed: bool = False) -> int:
    """The 32-bit value in halfwords ``number`` and ``number + 1``."""
    offset = (number - 10) * 2
    return struct.unpack_from(">i" if signed else ">I", block, offset)[0]


def read_sweep(data: bytes) -> Sweep:
    """Parse one Level 3 N0B / N0G file's bytes."""
    start = _skip_wmo_heading(data)
    description = data[start + _HEADER_BYTES : start + _HEADER_BYTES + _DESCRIPTION_BYTES]
    if len(description) != _DESCRIPTION_BYTES or _halfword(description, 10) != -1:
        raise NexradProductError("Level 3 file has no product description block")
    code = _halfword(description, 16)
    if code not in PRODUCTS:
        raise NexradProductError(f"Level 3 product {code} is not one this product carries")
    product, gates, minimum, increment, levels = PRODUCTS[code]
    thresholds = (_halfword(description, 31), _halfword(description, 32), _halfword(description, 33))
    if thresholds != (minimum, increment, levels):
        raise NexradProductError(
            f"Level 3 {product} thresholds {thresholds} are not the codebook (min, step, levels) "
            f"{(minimum, increment, levels)}"
        )
    days = _halfword(description, 21)
    seconds = _word(description, 22)
    scan_time = _EPOCH + timedelta(days=days, seconds=seconds)

    body = data[start + _HEADER_BYTES + _DESCRIPTION_BYTES :]
    compression = _halfword(description, 51)
    if compression == 1:
        try:
            body = bz2.decompress(body)
        except (OSError, ValueError) as exc:
            raise NexradProductError(f"Level 3 {product} symbology is not bzip2: {exc}") from exc
    elif compression != 0:
        raise NexradProductError(f"Level 3 {product} uses compression method {compression}")
    codes = _read_radials(body, gates, product)
    return Sweep(
        product=product,
        site_latitude=_word(description, 11, signed=True) / 1000.0,
        site_longitude=_word(description, 13, signed=True) / 1000.0,
        site_height_m=_halfword(description, 15) * 0.3048,
        scan_time=scan_time,
        elevation=_halfword(description, 30) / 10.0,
        vcp=_halfword(description, 18),
        volume_number=_halfword(description, 20),
        elevation_number=_halfword(description, 29),
        codes=codes,
    )


def _read_radials(body: bytes, gates: int, product: str) -> np.ndarray:
    """The symbology block's one packet 16, binned by beam centre onto the
    fixed 0.5° grid."""
    if len(body) < 16 or struct.unpack_from(">hh", body, 0) != (-1, 1):
        raise NexradProductError(f"Level 3 {product} has no product symbology block")
    layers = struct.unpack_from(">h", body, 8)[0]
    if layers != 1 or struct.unpack_from(">h", body, 10)[0] != -1:
        raise NexradProductError(f"Level 3 {product} symbology must hold exactly one layer")
    offset = 16  # past the block header (10 bytes) and the layer header (6)
    packet = struct.unpack_from(">H", body, offset)[0]
    if packet != _PACKET_DIGITAL_RADIAL:
        raise NexradProductError(f"Level 3 {product} carries packet {packet}, not a digital radial array")
    first_bin, bins, _i, _j, _scale, radials = struct.unpack_from(">hhhhhh", body, offset + 2)
    if first_bin != 0 or bins != gates:
        raise NexradProductError(f"Level 3 {product} has {bins} gates from {first_bin}, not {gates} from 0")
    codes = np.zeros((BEAMS, gates), dtype=np.uint8)
    filled = np.zeros(BEAMS, dtype=bool)
    position = offset + 14
    for _ in range(radials):
        length, start_angle, delta_angle = struct.unpack_from(">hhh", body, position)
        position += 6
        values = np.frombuffer(body, dtype=np.uint8, count=gates, offset=position)
        position += length
        centre = (start_angle + delta_angle / 2.0) / 10.0 % 360.0
        beam = int(centre // BEAM_WIDTH) % BEAMS
        if filled[beam]:
            raise NexradProductError(f"Level 3 {product} has two radials in beam {beam}")
        filled[beam] = True
        codes[beam] = values
    return codes
