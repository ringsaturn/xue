"""Writing and reading one polar store (``docs/zarr-profile.md``, "Polar
store"): one product over one round (a round store) or every round of a
closed window (a window store), a Zarr v3 group holding the ``[site, scan,
azimuth, range]`` array as one shard and its coordinates. The inner chunk
is one site's sweeps of one round, so a window store's site is read round
by round exactly as a round store's is.

The shard, its index and the codec chain are the grid stores' own
(``zarrstore``): one ``(offset, nbytes)`` pair per inner chunk and the
CRC-32C of the pairs, at the end, and ``[bytes, zstd]`` with a content
checksum. Only the dimensions, the chunking and the attributes differ. The
reader here is NumPy-only, the way ``zarrstore.read_plane`` is, so the
tests read back without zarr-python; a stock Zarr client reads the same
store (``tests/test_nexrad.py`` checks that when zarr-python is installed).
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from .. import zstdcli
from ..errors import NexradProductError
from ..zarrstore import ZSTD_LEVEL, crc32c
from .level3 import BEAM_WIDTH, BEAMS, PRODUCTS, Sweep

POLAR_VERSION = 1
DIMENSIONS = ("site", "scan", "azimuth", "range")
GATE_KM = 0.25
FILL_VALUE = 0
SCAN_PADDING = -1

_INDEX_ENTRY = struct.Struct("<QQ")
_EMPTY_ENTRY = 0xFFFFFFFFFFFFFFFF
"""Both halves of a never-written chunk's index pair."""
_INDEX_CHECKSUM = struct.Struct("<I")
_CODEC_BYTES = {"name": "bytes"}
_CODEC_BYTES_LITTLE = {"name": "bytes", "configuration": {"endian": "little"}}
_CODEC_ZSTD = {"name": "zstd", "configuration": {"level": ZSTD_LEVEL, "checksum": True}}
_CODEC_CRC32C = {"name": "crc32c"}

VARIABLES: dict[str, dict[str, Any]] = {
    "n0b": {
        "id": "n0b",
        "label": "Base reflectivity",
        "unit": "dBZ",
        "quantization": {"type": "linear", "offset": -33.0, "scale": 0.5, "minimumCode": 2, "maximumCode": 255},
        "reservedCodes": {"0": "below_threshold", "1": "range_folded"},
    },
    "n0g": {
        "id": "n0g",
        "label": "Base velocity",
        "unit": "m/s",
        "quantization": {"type": "linear", "offset": -64.5, "scale": 0.5, "minimumCode": 2, "maximumCode": 255},
        "reservedCodes": {"0": "below_threshold", "1": "range_folded"},
        "signConvention": "positive_away",
    },
}
"""The two products' codebooks, ``docs/nexrad.md`` §2. ``offset`` is the
source's ``minimum − 2 × increment``: code 2 is the minimum."""


def gates_for(product: str) -> int:
    return next(gates for (name, gates, *_rest) in PRODUCTS.values() if name == product)


@dataclass(frozen=True)
class SiteSweeps:
    """One site's sweeps of one product in one round, oldest first."""

    site: str
    latitude: float
    longitude: float
    height_m: float
    sweeps: list[Sweep]


@dataclass(frozen=True)
class StoreReport:
    """What the window manifest records of a round store just written."""

    group_bytes: int
    group_crc32: str
    shard_bytes: int
    shard_crc32: str
    chunks: list[tuple[str, int, int, int]]
    """``(site, offset, length, sweeps)`` per site, in store order."""


@dataclass(frozen=True)
class WindowStoreReport:
    """What the window manifest records of a window store just written."""

    group_bytes: int
    group_crc32: str
    shard_bytes: int
    shard_crc32: str
    depth: int
    """The inner chunk's sweep slots: the busiest site's busiest round."""
    rounds: list[list[tuple[str, int, int, int]]]
    """Per round, ``(site, offset, length, sweeps)`` for every site with a
    sweep in it, in store order."""


def _dump_json(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _crc32_hex(data: bytes) -> str:
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coordinate(values: np.ndarray, data_type: str, fill_value: Any, dimensions: tuple[str, ...], attributes: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    metadata = {
        "zarr_format": 3,
        "node_type": "array",
        "shape": list(values.shape),
        "data_type": data_type,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": list(values.shape)}},
        "chunk_key_encoding": {"name": "default"},
        "fill_value": fill_value,
        "codecs": [_CODEC_BYTES_LITTLE],
        "attributes": attributes,
        "dimension_names": list(dimensions),
    }
    return metadata, values.astype(values.dtype.newbyteorder("<")).tobytes()


def write_store(zarr_dir: Path, *, product: str, round_time: datetime, sites: list[SiteSweeps]) -> StoreReport:
    """Write ``<product>.zarr`` for one round from each site's sweeps, the
    sites in the order given (the window's site table order)."""
    if not sites or any(not entry.sweeps for entry in sites):
        raise NexradProductError("a polar store holds only sites with at least one sweep")
    report = _write_polar(zarr_dir, product=product, rounds=[round_time], sites=sites, cells={(row, 0): entry.sweeps for row, entry in enumerate(sites)})
    return StoreReport(
        group_bytes=report.group_bytes,
        group_crc32=report.group_crc32,
        shard_bytes=report.shard_bytes,
        shard_crc32=report.shard_crc32,
        chunks=report.rounds[0],
    )


def write_window_store(zarr_dir: Path, *, product: str, rounds: list[datetime], sites: list[SiteSweeps]) -> WindowStoreReport:
    """Write ``<product>.zarr`` for a closed window: every site's sweeps,
    oldest first, each filed under the first of ``rounds`` at or after its
    start. A sweep after the last round, or one no round reaches, is
    refused rather than dropped."""
    if not rounds or any(later <= earlier for earlier, later in zip(rounds, rounds[1:])):
        raise NexradProductError("a window store's rounds must be oldest first and unique")
    if not sites or any(not entry.sweeps for entry in sites):
        raise NexradProductError("a polar store holds only sites with at least one sweep")
    cells: dict[tuple[int, int], list[Sweep]] = {}
    for row, entry in enumerate(sites):
        for sweep in entry.sweeps:
            column = next((position for position, moment in enumerate(rounds) if sweep.scan_time <= moment), None)
            if column is None:
                raise NexradProductError(f"{entry.site}: a sweep at {_iso(sweep.scan_time)} is after the window's last round")
            cells.setdefault((row, column), []).append(sweep)
    return _write_polar(zarr_dir, product=product, rounds=rounds, sites=sites, cells=cells)


def _write_polar(
    zarr_dir: Path,
    *,
    product: str,
    rounds: list[datetime],
    sites: list[SiteSweeps],
    cells: dict[tuple[int, int], list[Sweep]],
) -> WindowStoreReport:
    """The store itself: inner chunk ``[s, r]`` is ``cells[(s, r)]``, the
    sweeps of site ``s`` in round ``r``, padded to the busiest cell. One
    round makes a round store, its block naming ``round``; more make a
    window store, naming ``rounds``."""
    if product not in VARIABLES:
        raise NexradProductError(f"unknown nexrad product {product!r}")
    gates = gates_for(product)
    depth = max(len(sweeps) for sweeps in cells.values())
    window = len(rounds) > 1

    entries = bytearray()
    payloads: list[bytes] = []
    chunks: list[list[tuple[str, int, int, int]]] = [[] for _ in rounds]
    cursor = 0
    for row, entry in enumerate(sites):
        for column in range(len(rounds)):
            sweeps = cells.get((row, column))
            if not sweeps:
                entries += _INDEX_ENTRY.pack(_EMPTY_ENTRY, _EMPTY_ENTRY)
                continue
            block = np.zeros((1, depth, BEAMS, gates), dtype=np.uint8)
            for position, sweep in enumerate(sweeps):
                if sweep.product != product or sweep.codes.shape != (BEAMS, gates):
                    raise NexradProductError(f"{entry.site}: a {sweep.product} sweep in the {product} store")
                block[0, position] = sweep.codes
            payload = zstdcli.compress(block.tobytes(), level=ZSTD_LEVEL, checksum=True)
            entries += _INDEX_ENTRY.pack(cursor, len(payload))
            chunks[column].append((entry.site, cursor, len(payload), len(sweeps)))
            payloads.append(payload)
            cursor += len(payload)
    shard = b"".join(payloads) + bytes(entries) + _INDEX_CHECKSUM.pack(crc32c(bytes(entries)))
    # Within a round the spans follow the site order, which the manifest
    # relies on: the shard is laid out site-major, so they do.
    scans = len(rounds) * depth

    variable = VARIABLES[product]
    quantization = variable["quantization"]
    array = {
        "zarr_format": 3,
        "node_type": "array",
        "shape": [len(sites), scans, BEAMS, gates],
        "data_type": "uint8",
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [len(sites), scans, BEAMS, gates]}},
        "chunk_key_encoding": {"name": "default"},
        "fill_value": FILL_VALUE,
        "codecs": [
            {
                "name": "sharding_indexed",
                "configuration": {
                    "chunk_shape": [1, depth, BEAMS, gates],
                    "codecs": [_CODEC_BYTES, _CODEC_ZSTD],
                    "index_codecs": [_CODEC_BYTES_LITTLE, _CODEC_CRC32C],
                    "index_location": "end",
                },
            }
        ],
        "attributes": {
            "xue_polar": {
                "variable": variable,
                "geometry": {"azimuthStart": 0.0, "azimuthStep": BEAM_WIDTH, "rangeStart": 0.0, "rangeStep": GATE_KM},
            },
            "scale_factor": quantization["scale"],
            "add_offset": quantization["offset"],
            "flag_values": [0, 1],
            "flag_meanings": "below_threshold range_folded",
        },
        "dimension_names": list(DIMENSIONS),
    }

    scan_time = np.full((len(sites), scans), SCAN_PADDING, dtype=np.int64)
    elevation = np.full((len(sites), scans), np.nan, dtype=np.float32)
    for (row, column), sweeps in cells.items():
        for position, sweep in enumerate(sweeps):
            scan_time[row, column * depth + position] = int(sweep.scan_time.timestamp())
            elevation[row, column * depth + position] = sweep.elevation
    for row, entry in enumerate(sites):
        real = scan_time[row][scan_time[row] != SCAN_PADDING]
        if np.any(np.diff(real) <= 0):
            raise NexradProductError(f"{entry.site}: sweeps must be in strictly increasing time")
    coordinates = {
        "azimuth": _coordinate(((np.arange(BEAMS) + 0.5) * BEAM_WIDTH).astype(np.float32), "float32", "NaN", ("azimuth",), {"units": "degrees", "long_name": "beam centre azimuth, clockwise from true north"}),
        "range": _coordinate(((np.arange(gates) + 0.5) * GATE_KM).astype(np.float32), "float32", "NaN", ("range",), {"units": "km", "long_name": "gate centre slant range"}),
        "site_latitude": _coordinate(np.array([s.latitude for s in sites], dtype=np.float64), "float64", "NaN", ("site",), {"units": "degrees_north"}),
        "site_longitude": _coordinate(np.array([s.longitude for s in sites], dtype=np.float64), "float64", "NaN", ("site",), {"units": "degrees_east"}),
        "site_height": _coordinate(np.array([s.height_m for s in sites], dtype=np.float32), "float32", "NaN", ("site",), {"units": "m", "long_name": "antenna height above mean sea level"}),
        "scan_time": _coordinate(scan_time, "int64", SCAN_PADDING, ("site", "scan"), {"units": "seconds since 1970-01-01T00:00:00Z", "long_name": "sweep start; -1 for a padding sweep"}),
        "elevation": _coordinate(elevation, "float32", "NaN", ("site", "scan"), {"units": "degrees"}),
    }

    _write(zarr_dir / product / "c" / "0" / "0" / "0" / "0", shard)
    _write(zarr_dir / product / "zarr.json", _dump_json(array))
    for name, (metadata, data) in coordinates.items():
        key = ("c", *(["0"] * len(metadata["shape"])))
        _write(zarr_dir / name / Path(*key), data)
        _write(zarr_dir / name / "zarr.json", _dump_json(metadata))
    block: dict[str, Any] = {"version": POLAR_VERSION, "product": product}
    if window:
        block["rounds"] = [_iso(moment) for moment in rounds]
    else:
        block["round"] = _iso(rounds[0])
    block["sites"] = [entry.site for entry in sites]
    group = {
        "zarr_format": 3,
        "node_type": "group",
        "attributes": {"xue_polar": block},
        "consolidated_metadata": {
            "kind": "inline",
            "must_understand": False,
            "metadata": {product: array, **{name: metadata for name, (metadata, _data) in coordinates.items()}},
        },
    }
    group_bytes = _dump_json(group)
    _write(zarr_dir / "zarr.json", group_bytes)
    return WindowStoreReport(
        group_bytes=len(group_bytes),
        group_crc32=_crc32_hex(group_bytes),
        shard_bytes=len(shard),
        shard_crc32=_crc32_hex(shard),
        depth=depth,
        rounds=chunks,
    )


def read_site(zarr_dir: Path, product: str, site_index: int, round_index: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """One site's real sweeps of one of the store's rounds and their start
    times, through the shard index: ``(codes [sweeps, 720, gates],
    scan_time [sweeps])``. A round the site has no sweep in reads empty."""
    group = json.loads((zarr_dir / "zarr.json").read_text(encoding="utf-8"))
    block = group.get("attributes", {}).get("xue_polar")
    if not isinstance(block, dict) or block.get("version") != POLAR_VERSION or "xue" in group.get("attributes", {}):
        raise NexradProductError(f"{zarr_dir} is not a version {POLAR_VERSION} polar store")
    if ("round" in block) == ("rounds" in block):
        raise NexradProductError(f"{zarr_dir}: a polar store names its round or its rounds, not both or neither")
    rounds = len(block["rounds"]) if "rounds" in block else 1
    if not 0 <= round_index < rounds:
        raise NexradProductError(f"{zarr_dir} has no round {round_index}")
    array = json.loads((zarr_dir / product / "zarr.json").read_text(encoding="utf-8"))
    sites, scans, beams, gates = array["shape"]
    depth = array["codecs"][0]["configuration"]["chunk_shape"][1]
    if depth * rounds != scans:
        raise NexradProductError(f"{zarr_dir}: scan is not its rounds times the chunk depth")
    shard = (zarr_dir / product / "c" / "0" / "0" / "0" / "0").read_bytes()
    index_length = _INDEX_ENTRY.size * sites * rounds + _INDEX_CHECKSUM.size
    index = shard[-index_length:]
    pairs = index[:-_INDEX_CHECKSUM.size]
    if _INDEX_CHECKSUM.unpack(index[-_INDEX_CHECKSUM.size :])[0] != crc32c(pairs):
        raise NexradProductError(f"{zarr_dir}: shard index checksum mismatch")
    times = np.frombuffer((zarr_dir / "scan_time" / "c" / "0" / "0").read_bytes(), dtype="<i8").reshape(sites, scans)[site_index]
    times = times[round_index * depth : (round_index + 1) * depth]
    real = int(np.count_nonzero(times != SCAN_PADDING))
    offset, nbytes = _INDEX_ENTRY.unpack_from(pairs, (site_index * rounds + round_index) * _INDEX_ENTRY.size)
    if offset == _EMPTY_ENTRY and nbytes == _EMPTY_ENTRY:
        return np.zeros((0, beams, gates), dtype=np.uint8), times[:0]
    raw = zstdcli.decompress(shard[offset : offset + nbytes], expected_length=depth * beams * gates)
    codes = np.frombuffer(raw, dtype=np.uint8).reshape(depth, beams, gates)
    return codes[:real], times[:real]
